"""Private immutable images/PDFs, bounded previews and scoped agent access."""

import base64
import hashlib
import io
import json
import struct
import zlib
from dataclasses import replace

import pytest
from test_public_inference import QUOTES
from test_public_server import installation as installation
from test_public_server import invited, structure

from reasoning_ledger.server.media_context import attachment_context, authorize_captured_media, narrow_attachment_snapshot
from reasoning_ledger.server.research_media import CHUNK_BYTES, ResearchMedia, bounded_image
from reasoning_ledger.server.store import StoreError


def png_chunk(kind, body):
    return len(body).to_bytes(4, "big") + kind + body + (zlib.crc32(kind + body) & 0xFFFFFFFF).to_bytes(4, "big")


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    + png_chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\xff"))
    + png_chunk(b"IEND", b"")
)
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
# Minimal SOF header fixture; pixel decoding is tested separately in the browser with real image bytes.
JPEG_HEADER = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xd9"


def upload(media, owner, topic, body=PNG, mime="image/png", name="picture.png", **previews):
    user, org = owner["user_id"], owner["organization_id"]
    start = media.begin(user, org, topic, name, mime, len(body), hashlib.sha256(body).hexdigest())
    for offset in range(0, len(body), CHUNK_BYTES):
        media.chunk(user, org, topic, start["id"], offset // CHUNK_BYTES, base64.b64encode(body[offset : offset + CHUNK_BYTES]).decode())
    return media.finish(user, org, topic, start["id"], **previews)


def text_pdf(text="An exact paper with a readable proof.", *, pages=1, password=None):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                               NameObject("/Subtype"): NameObject("/Type1"),
                                               NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for _ in range(pages):
        page = writer.add_blank_page(612, 792)
        if text:
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 20 700 Td (" + text.encode("ascii") + b") Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


def test_server_pdf_extraction_reads_original_bytes_beyond_the_browser_preview_without_a_write_lock(installation, monkeypatch):
    import sqlite3

    from reasoning_ledger.server.pdf_extract import extract_pdf

    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][1]["id"]
    body = text_pdf("A complete paragraph. " * 250, pages=3)

    def unlocked(original):
        assert original == body
        with sqlite3.connect(store.database, timeout=0.05) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        return extract_pdf(original)

    monkeypatch.setattr("reasoning_ledger.server.pdf_extract.extract_pdf", unlocked)
    saved = upload(ResearchMedia(store), owner, topic, body, "application/pdf", "paper.pdf")
    assert saved["extraction"]["status"] == "ready" and saved["extraction"]["pages_read"] == 3
    media = ResearchMedia(store)
    value = media.read(user, org, topic, saved["id"], saved["sha256"], offset=16000)
    assert value["total_characters"] > 16000 and value["text"]
    assert value["text_status"] == "server_extracted"
    assert client.get(saved["url"]).content == body
    assert media.finish(user, org, topic, saved["id"]) == saved


@pytest.mark.parametrize("body,error", [(text_pdf(""), "no_text"), (text_pdf(password="private-password"), "password_required"),
                                        (PDF, "extraction_failed")])
def test_unreadable_pdf_upload_retains_original_and_explains_the_reading_failure(installation, body, error):
    _, store, client, owner = installation
    topic = structure(client, owner["organization_id"])[2][1]["id"]
    saved = upload(ResearchMedia(store), owner, topic, body, "application/pdf", "unreadable.pdf")
    assert saved["extraction"]["status"] in {"unavailable", "failed"}
    assert saved["extraction"]["error"] == error
    assert client.get(saved["url"]).content == body
    retried = client.post(saved["url"].replace("/content", "/extract"), json={})
    assert retried.status_code == 200 and retried.json()["extraction"]["error"] == error


def test_pdf_text_limit_reports_partial_coverage_and_keeps_readable_text(monkeypatch):
    from reasoning_ledger.server import pdf_extract_worker

    monkeypatch.setattr(pdf_extract_worker, "MAX_TEXT_BYTES", 100)
    value = pdf_extract_worker.extract(text_pdf("A retained proof paragraph. " * 100, pages=2))
    assert value["status"] == "partial" and value["truncated"] is True
    assert 0 < len(value["text"].encode()) <= 100 and value["total_pages"] == 2


def test_pdf_extractor_subprocess_reads_text_on_this_platform():
    from reasoning_ledger.server.pdf_extract import extract_pdf

    value = extract_pdf(text_pdf("A readable theorem statement.", pages=2))
    assert value["status"] == "ready" and "A readable theorem statement." in value["text"], value
    assert value["pages_read"] == value["total_pages"] == 2


@pytest.mark.parametrize("platform,fatal", [("darwin", False), ("linux", True)])
def test_pdf_worker_skips_its_memory_limit_only_where_the_platform_refuses_every_one(monkeypatch, platform, fatal):
    from reasoning_ledger.server import pdf_extract_worker

    class Refusing:
        RLIMIT_AS = 9

        def setrlimit(self, which, limits):
            raise ValueError("current limit exceeds maximum limit")

    monkeypatch.setattr(pdf_extract_worker.sys, "platform", platform)
    if fatal:
        with pytest.raises(ValueError):
            pdf_extract_worker.limit_memory(Refusing())
    else:
        pdf_extract_worker.limit_memory(Refusing())


def test_pdf_extractor_timeout_is_bounded_and_contains_no_input_or_exception_body(monkeypatch):
    import subprocess

    from reasoning_ledger.server import pdf_extract

    secret = "private proof and parser details"

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 12
        raise subprocess.TimeoutExpired(secret, 12, output=secret)

    monkeypatch.setattr(pdf_extract.subprocess, "run", timeout)
    value = pdf_extract.extract_pdf(secret.encode())
    assert value == {"status": "failed", "error": "extraction_timeout", "text": ""}
    assert secret not in json.dumps(value)


def test_upload_and_embedding_routes_all_topic_kinds_with_exact_hashes(installation):
    _, store, client, owner = installation
    org = owner["organization_id"]
    for topic in structure(client, org)[2]:
        root = f"/api/organizations/{org}/topics/{topic['id']}/media"
        sha = hashlib.sha256(PNG).hexdigest()
        start = client.post(root, json={"name": "plot.png", "mime": "image/png", "size_bytes": len(PNG), "sha256": sha})
        assert start.status_code == 200, start.text
        path = root + "/" + start.json()["id"]
        assert client.get(path + f"/{sha}/content").status_code == 404
        chunk = {"data_base64": base64.b64encode(PNG).decode()}
        assert client.put(path + "/chunks/0", json=chunk).status_code == 200
        assert client.put(path + "/chunks/0", json=chunk).status_code == 200
        result = client.post(path + "/finish", json={}).json()
        assert result["sha256"] == sha and result["url"] in result["markdown"]
        assert client.post(path + "/finish", json={}).json() == result
        blob = client.get(result["url"])
        assert blob.status_code == 200 and blob.content == PNG
        assert blob.headers["content-type"] == "image/png" and blob.headers["cache-control"] == "no-store"
        assert blob.headers["content-security-policy"] == "default-src 'none'; sandbox; frame-ancestors 'none'"
        assert "filename*=UTF-8''plot.png" in blob.headers["content-disposition"]
        assert client.get(result["url"].replace(sha, "0" * 64)).status_code == 404
        assert client.get(root).json() == [result]
        exported = client.get(f"/api/organizations/{org}/topics/{topic['id']}/export").json()
        assert exported["attachments"] == [result]
        assert "vision_base64" not in str(exported["attachments"])
        assert "data_base64" not in str(exported["attachments"])


def test_current_origin_topic_acl_blocks_sibling_private_foreign_and_viewer_writes(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    public = structure(client, org)[2][0]["id"]
    project = store.create_entity(user, org, "projects", "Private", visibility="private", owner_id=user)
    channel = store.create_entity(user, org, "channels", "secret", project["id"])
    private = store.create_entity(user, org, "topics", "private", channel["id"], "alpha")["id"]
    media = ResearchMedia(store)
    result = upload(media, owner, private)
    _, member_client, member = invited(installation)
    assert member_client.get(result["url"]).status_code in (403, 404)
    assert client.get(result["url"].replace(private, public)).status_code == 404
    assert client.get(result["url"].replace(org, "f" * 32)).status_code in (403, 404)
    _, viewer_client, _ = invited(installation, role="viewer", email="view@example.org")
    root = f"/api/organizations/{org}/topics/{public}/media"
    assert (
        viewer_client.post(
            root, json={"name": "x.png", "mime": "image/png", "size_bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}
        ).status_code
        == 403
    )
    with store.transaction() as db:
        messages, references, _ = attachment_context(store, db, member["user_id"], org, [{"content": result["markdown"]}], QUOTES, 90000)
    assert messages == references == []


def test_pending_upload_is_owner_bound_and_chunks_are_immutable(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][0]["id"]
    _, _, member = invited(installation)
    media = ResearchMedia(store)
    start = media.begin(user, org, topic, "x.png", "image/png", len(PNG), hashlib.sha256(PNG).hexdigest())
    chunk = base64.b64encode(PNG).decode()
    with pytest.raises(StoreError):
        media.chunk(member["user_id"], org, topic, start["id"], 0, chunk)
    with pytest.raises(StoreError, match="incomplete"):
        media.finish(user, org, topic, start["id"])
    media.chunk(user, org, topic, start["id"], 0, chunk)
    with pytest.raises(StoreError, match="cannot be replaced"):
        media.chunk(user, org, topic, start["id"], 0, base64.b64encode(b"x" * len(PNG)).decode())
    result = media.finish(user, org, topic, start["id"])
    assert media.read(user, org, topic, result["id"], result["sha256"], binary=True)[1] == PNG


def test_pdf_previews_are_bounded_explicitly_untrusted_and_never_execute_tex(installation):
    _, store, client, owner = installation
    topic = structure(client, owner["organization_id"])[2][1]["id"]
    media = ResearchMedia(store)
    result = upload(media, owner, topic, PDF, "application/pdf", "proof.pdf", preview_text="Extracted page 1\nA proof.")
    read = media.read(owner["user_id"], owner["organization_id"], topic, result["id"], result["sha256"], offset=0, length=10)
    assert read["text"] == "Extracted " and read["next_offset"] == 10
    assert "unverified" in read["source"] and "preview only" in read["inspection"]
    with store.transaction() as db:
        messages, refs, _ = attachment_context(
            store, db, owner["user_id"], owner["organization_id"], [{"content": result["markdown"]}], QUOTES, 90000
        )
    assert "A proof." in messages[0]["content"] and refs[0]["sha256"] == result["sha256"]
    assert "text" not in refs[0]


def test_image_context_requires_vision_quote_and_captures_projection_hash(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][1]["id"]
    result = upload(ResearchMedia(store), owner, topic, vision_base64=base64.b64encode(JPEG_HEADER).decode())
    with store.transaction() as db:
        source = [{"content": result["markdown"]}]
        text, refs, _ = attachment_context(store, db, user, org, source, QUOTES, 90000)
        vision, inspected, size = attachment_context(store, db, user, org, source, [replace(QUOTES[0], supports_images=True)], 90000)
        tiny, _, _ = attachment_context(store, db, user, org, source, QUOTES, 8)
    assert len(text) == 1 and "pixels have not been inspected" in refs[0]["inspection"]
    assert vision[1]["content"][1]["type"] == "image_url" and inspected[0]["preview_sha256"] == hashlib.sha256(JPEG_HEADER).hexdigest()
    assert 0 < size <= 90000 and tiny == []
    priced = replace(QUOTES[0], pricing={"image": "0.01"})
    assert priced.image_reservation(vision) == 10000
    oversized = JPEG_HEADER[:9] + (513).to_bytes(2, "big") + JPEG_HEADER[11:]
    with pytest.raises(StoreError, match="512 pixels"):
        upload(ResearchMedia(store), owner, topic, vision_base64=base64.b64encode(oversized).decode())


NOTES = "# Lemma 2\n\nIf $V$ is finite-dimensional, then $\\dim\\ker T + \\dim\\operatorname{im} T = \\dim V$. Café.\n".encode()


def test_markdown_upload_is_its_own_text_for_reading_and_models(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][0]["id"]
    media = ResearchMedia(store)
    # The uploaded bytes are the text; a different uploader preview is never substituted for them.
    result = upload(media, owner, topic, NOTES, "text/markdown", "notes.md", preview_text="An uploader's summary.")
    assert result["mime"] == "text/markdown" and result["markdown"] == f"![notes.md]({result['url']})"
    assert result["extraction"] == {"status": "ready", "method": "text", "truncated": False}
    read = media.read(user, org, topic, result["id"], result["sha256"])
    assert read["text"] == NOTES.decode() and read["inspection"] == "Markdown source text as uploaded"
    blob = client.get(result["url"])
    assert blob.content == NOTES and blob.headers["content-type"] == "text/markdown; charset=utf-8"
    assert blob.headers["content-security-policy"] == "default-src 'none'; sandbox; frame-ancestors 'none'"
    with store.transaction() as db:
        messages, references, _ = attachment_context(store, db, user, org, [{"content": result["markdown"]}], QUOTES[:1], 100000)
    assert references[0]["sha256"] == result["sha256"] and "Café" in messages[0]["content"]
    assert "finite-dimensional" in messages[0]["content"]


def test_markdown_must_be_utf8_without_nul_and_its_model_text_is_bounded(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][0]["id"]
    media = ResearchMedia(store)
    for body in (b"caf\xe9 latin-1", b"text\x00with NUL", PNG):
        with pytest.raises(StoreError, match="image, PDF or Markdown format"):
            upload(media, owner, topic, body, "text/markdown", "bad.md")
    # A multibyte character straddling the 2 MiB boundary is dropped whole, never split.
    long = ("x" * (2 * 1024 * 1024 - 1) + "é" + "y" * 1000).encode()
    result = upload(media, owner, topic, long, "text/markdown", "long.md")
    assert result["extraction"]["truncated"] is True
    text = media.read(user, org, topic, result["id"], result["sha256"])
    assert text["total_characters"] == 2 * 1024 * 1024 - 1
    assert client.get(result["url"]).content == long, "the original bytes stay complete"


def test_bad_formats_hashes_oversized_images_and_paths_are_refused(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    topic = structure(client, org)[2][0]["id"]
    media = ResearchMedia(store)
    with pytest.raises(StoreError):
        media.begin(user, org, topic, "../file.svg", "image/svg+xml", 1, "0" * 64)
    with pytest.raises(StoreError):
        media.begin(user, org, topic, "[malicious].png", "image/png", 1, "0" * 64)
    with pytest.raises(StoreError, match="UTF-8"):
        media.begin(user, org, topic, "bad\ud800.png", "image/png", 1, "0" * 64)
    with pytest.raises(StoreError):
        media.finish(user, org, topic, "a" * 32, preview_text="\ud800")
    with pytest.raises(StoreError, match="format"):
        upload(media, owner, topic, b"<html>not an image</html>")
    bomb = PNG[:16] + (100000).to_bytes(4, "big") + (100000).to_bytes(4, "big") + PNG[24:]
    assert not bounded_image(bomb, "image/png")
    with pytest.raises(StoreError, match="format"):
        upload(media, owner, topic, bomb)
    start = media.begin(user, org, topic, "wrong.png", "image/png", len(PNG), "0" * 64)
    media.chunk(user, org, topic, start["id"], 0, base64.b64encode(PNG).decode())
    with pytest.raises(StoreError, match="checksum"):
        media.finish(user, org, topic, start["id"])


def test_bot_or_agent_authorization_is_rechecked_inside_each_media_transaction(installation):
    _, store, client, owner = installation
    topic = structure(client, owner["organization_id"])[2][0]["id"]

    def denied(db):
        raise StoreError(409, "lease no longer active")

    media = ResearchMedia(store, authorize=denied)
    with pytest.raises(StoreError, match="lease"):
        upload(media, owner, topic)


def test_explicit_same_project_source_reads_and_context_reject_cross_project_media(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    project, _, topics = structure(client, org)
    media = ResearchMedia(store)
    result = upload(media, owner, topics[0]["id"])
    assert (
        media.read(user, org, topics[1]["id"], result["id"], result["sha256"], source_topic_id=topics[0]["id"])["sha256"]
        == result["sha256"]
    )
    foreign = store.create_entity(user, org, "projects", "Another project")
    channel = store.create_entity(user, org, "channels", "other", foreign["id"])
    topic = store.create_entity(user, org, "topics", "other", channel["id"], "beta")["id"]
    other = upload(media, owner, topic)
    with pytest.raises(StoreError, match="cross project"):
        media.read(user, org, topics[1]["id"], other["id"], other["sha256"], source_topic_id=topic)
    with store.transaction() as db:
        messages, refs, _ = attachment_context(store, db, user, org, [{"content": other["markdown"]}], QUOTES, 90000, project=project["id"])
    assert messages == refs == []


def test_native_image_provider_payload_captures_exact_context_and_settles_usage(installation):
    import json

    from test_public_inference import MODELS, prepare
    from test_public_research_agent import ResearchFixture

    from reasoning_ledger.server.research_agent import ResearchAgent

    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    attachment = upload(ResearchMedia(state.store), owner, topic, vision_base64=base64.b64encode(JPEG_HEADER).decode())
    state.store.post_message(user, org, topic, "Explain the picture. " + attachment["markdown"], "picture-question")
    quote = replace(QUOTES[0], supports_images=True, supports_tools=True, pricing={"image": "0.001"})
    state.enqueue(user, org, topic, "picture-execution", state.parameters([MODELS[0]], 256, 50, None), [quote])
    row = state.claim()
    fixture = ResearchFixture("answer")
    ResearchAgent(state, fixture.provider, row, quote).execute()
    result = state.run(user, org, row["id"])
    assert result["status"] == "succeeded" and result["actual_microusd"] == 8
    assert any(
        isinstance(message["content"], list) and message["content"][1]["type"] == "image_url" for message in fixture.requests[0]["messages"]
    )
    with state.store.transaction() as db:
        snapshot = json.loads(
            db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (row["request_id"],)).fetchone()[0]
        )
    assert snapshot["attachments"][0]["sha256"] == attachment["sha256"]
    assert snapshot["attachments"][0]["preview_sha256"] == hashlib.sha256(JPEG_HEADER).hexdigest()


def test_captured_media_is_refused_before_provider_submission_after_topic_revocation(installation):
    from test_public_inference import MODELS, prepare
    from test_public_research_agent import ResearchFixture

    from reasoning_ledger.server.research_agent import ResearchAgent

    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    attachment = upload(ResearchMedia(state.store), owner, topics[0]["id"])
    state.store.post_message(user, org, topic, "Read picture " + attachment["markdown"], "captured-question")
    quote = replace(QUOTES[0], supports_tools=True)
    state.enqueue(user, org, topic, "revoked-picture", state.parameters([MODELS[0]], 256, 50, None), [quote])
    row = state.claim()
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE topics SET deleted_at=1 WHERE id=?", (topics[0]["id"],))
    fixture = ResearchFixture("answer")
    ResearchAgent(state, fixture.provider, row, quote).execute()
    assert fixture.requests == []
    assert state.run(user, org, row["id"])["status"] != "succeeded"


def test_gamma_workflow_media_is_narrow_idempotent_and_does_not_enable_tools(installation):
    import json

    from test_public_inference import MODELS, prepare
    from test_public_research_agent import ResearchFixture

    from reasoning_ledger.server.research_agent import ResearchAgent

    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[2]["id"]
    media = ResearchMedia(state.store)
    attachment = upload(media, owner, topic, vision_base64=base64.b64encode(JPEG_HEADER).decode())
    unrelated = upload(media, owner, topics[1]["id"], name="unrelated.png")
    state.store.post_message(user, org, topic, unrelated["markdown"], "unrelated-gamma-history")
    quote = replace(QUOTES[0], supports_images=True, supports_tools=True)
    params = state.parameters([MODELS[0]], 256, 1, None)
    context = [{"role": "system", "content": "Answer this comparison without tools."},
               {"role": "user", "content": "Explain " + attachment["markdown"]},
               {"role": "user", "content": "No dependency results."}]
    with state.store.transaction(write=True) as db:
        run = state._enqueue(db, user, org, topic, "gamma-media", params, [quote], workflow_context=context)[0]
    with state.store.transaction(write=True) as db:
        assert state._enqueue(db, user, org, topic, "gamma-media", params, [quote], workflow_context=context)[0]["id"] == run["id"]
    with state.store.transaction() as db:
        snapshot = json.loads(db.execute(
            "SELECT snapshot_json FROM research_contexts WHERE request_id=(SELECT request_id FROM llm_runs WHERE id=?)",
            (run["id"],),
        ).fetchone()[0])
        assert [item["id"] for item in snapshot["attachments"]] == [attachment["id"]]
        assert not db.execute("SELECT 1 FROM topic_agent_threads WHERE root_run=?", (run["id"],)).fetchone()
    with pytest.raises(StoreError, match="different model request"), state.store.transaction(write=True) as db:
        state._enqueue(db, user, org, topic, "gamma-media", params, [quote],
                       workflow_context=context + [{"role": "user", "content": "changed"}])
    row = state.claim()
    fixture = ResearchFixture("answer")
    ResearchAgent(state, fixture.provider, row, quote).execute()
    assert state.run(user, org, row["id"])["status"] == "succeeded"
    assert any(isinstance(message["content"], list) for message in fixture.requests[0]["messages"])
    assert not fixture.requests[0].get("tools"), "capturing workflow attachments must not grant research tools"


@pytest.mark.parametrize("referenced", [False, True])
def test_delegate_media_is_assignment_scoped_and_rechecks_only_supplied_references(installation, referenced):
    from test_public_inference import MODELS, prepare
    from test_public_research_agent import ResearchFixture

    from reasoning_ledger.server.research_agent import ResearchAgent
    from reasoning_ledger.server.topic_agents import TopicAgents

    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    attachment = upload(
        ResearchMedia(state.store),
        owner,
        topics[0]["id"],
        name="parent-only.png",
        vision_base64=base64.b64encode(JPEG_HEADER).decode(),
    )
    state.store.post_message(user, org, topic, "Parent secret conversation " + attachment["markdown"], "parent-question")
    quote = replace(QUOTES[0], supports_images=True, supports_tools=True)
    state.enqueue(user, org, topic, "parent-media", state.parameters([MODELS[0]], 256, 50, None), [quote])
    parent = state.claim()
    assigned = attachment["markdown"] if referenced else "Only the supplied abstract task."
    task = TopicAgents(state).delegate(parent, "scoped-media", "worker", "Investigate assignment", assigned)
    with state.store.transaction() as db:
        child = db.execute(state._joined() + "WHERE r.id=?", (task["child_run"],)).fetchone()
        snapshot = json.loads(
            db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (child["request_id"],)).fetchone()[0]
        )
        messages = json.loads(child["context_json"])
        original = json.loads(
            db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (parent["request_id"],)).fetchone()[0]
        )
    assert snapshot["messages"] == messages
    assert "Parent secret conversation" not in json.dumps(messages)
    assert snapshot["recent_sequences"] == snapshot["pinned_message_ids"] == []
    assert len(original["attachments"]) == 1
    assert len(snapshot["attachments"]) == int(referenced)
    assert any(isinstance(message["content"], list) for message in messages) == referenced
    if referenced:
        assert snapshot["attachments"][0]["preview_sha256"] == hashlib.sha256(JPEG_HEADER).hexdigest()
    else:
        assert "parent-only.png" not in json.dumps(snapshot)
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE topics SET deleted_at=1 WHERE id=?", (topics[0]["id"],))
    state.finish(parent, "failed", error="fixture_parent_complete")
    child = state.claim()
    assert child["id"] == task["child_run"]
    fixture = ResearchFixture("assignment result")
    ResearchAgent(state, fixture.provider, child, quote).execute()
    if referenced:
        assert fixture.requests == []
    else:
        assert fixture.requests and state.run(user, org, child["id"])["status"] == "succeeded"
        assert "parent-only.png" not in json.dumps(fixture.requests)


def test_provider_media_authorization_uses_actual_payload_not_unused_snapshot_receipts(installation):
    from test_public_inference import MODELS, prepare

    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    attachment = upload(ResearchMedia(state.store), owner, topics[0]["id"], vision_base64=base64.b64encode(JPEG_HEADER).decode())
    state.store.post_message(user, org, topic, attachment["markdown"], "authorization-question")
    quote = replace(QUOTES[0], supports_images=True, supports_tools=True)
    state.enqueue(user, org, topic, "authorization-media", state.parameters([MODELS[0]], 256, 50, None), [quote])
    row = state.claim()
    original = json.loads(row["context_json"])
    picture = next(message for message in original if isinstance(message["content"], list))
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE topics SET deleted_at=1 WHERE id=?", (topics[0]["id"],))
    authorize_captured_media(state.store, row, [{"role": "user", "content": "Unrelated assignment"}])
    for messages in ([{"role": "user", "content": attachment["markdown"]}], [picture]):
        with pytest.raises(StoreError):
            authorize_captured_media(state.store, row, messages)


def test_uncaptured_inaccessible_and_cross_project_links_do_not_block_plain_provider_context(installation):
    from test_public_inference import MODELS, prepare
    from test_public_research_agent import ResearchFixture

    from reasoning_ledger.server.research_agent import ResearchAgent

    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    media = ResearchMedia(state.store)
    revoked = upload(media, owner, topics[0]["id"])
    project = state.store.create_entity(user, org, "projects", "Other project")
    channel = state.store.create_entity(user, org, "channels", "other", project["id"])
    other_topic = state.store.create_entity(user, org, "topics", "other", channel["id"], "alpha")["id"]
    foreign = upload(media, owner, other_topic)
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE topics SET deleted_at=1 WHERE id=?", (topics[0]["id"],))
    state.store.post_message(user, org, topic, "Unavailable copied links " + revoked["markdown"] + foreign["markdown"], "plain-links")
    quote = replace(QUOTES[0], supports_images=True, supports_tools=True)
    state.enqueue(user, org, topic, "plain-link-context", state.parameters([MODELS[0]], 256, 50, None), [quote])
    row = state.claim()
    with state.store.transaction() as db:
        snapshot = json.loads(
            db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (row["request_id"],)).fetchone()[0]
        )
    assert snapshot["attachments"] == []
    fixture = ResearchFixture("No private attachment representation was supplied.")
    ResearchAgent(state, fixture.provider, row, quote).execute()
    assert fixture.requests and state.run(user, org, row["id"])["status"] == "succeeded"
    assert all(isinstance(message["content"], str) for message in fixture.requests[0]["messages"])


@pytest.mark.parametrize("discovery", [False, True])
@pytest.mark.parametrize("operation", ["media_read", "media_download_chunk"])
def test_native_media_read_captures_new_source_and_refuses_revoked_text_before_next_provider(installation, discovery, operation):
    from test_public_inference import MODELS, prepare
    from test_public_research_agent import ResearchFixture

    from reasoning_ledger.server.openrouter import ProviderError
    from reasoning_ledger.server.research_provider import ResearchCompletion
    from reasoning_ledger.server.research_tools import ResearchTools

    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    attachment = upload(
        ResearchMedia(state.store),
        owner,
        topics[0]["id"],
        body=PDF,
        mime="application/pdf",
        name="source.pdf",
        preview_text="Private PDF evidence",
    )
    quote = replace(QUOTES[0], supports_tools=True)
    state.enqueue(user, org, topic, "read-new-source", state.parameters([MODELS[0]], 256, 50, None), [quote])
    row = state.claim()
    arguments = {"identity": attachment["id"], "sha256": attachment["sha256"], "source_topic_id": topics[0]["id"]}
    tools = ResearchTools(state, row)
    result = (
        tools.invoke("tool_call", {"name": operation, "arguments": arguments}) if discovery else tools.invoke(operation, arguments)
    )
    if operation == "media_read":
        assert "Private PDF evidence" in json.dumps(result)
    else:
        retained = result["result"]["result"] if discovery else result["result"]
        assert base64.b64decode(retained["data_base64"]) == PDF
    with state.store.transaction() as db:
        snapshot = json.loads(
            db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (row["request_id"],)).fetchone()[0]
        )
        receipt = db.execute("SELECT status FROM research_tool_receipts WHERE id=?", (result["receipt_id"],)).fetchone()[0]
    assert receipt == "completed" and snapshot["attachments"][0]["id"] == attachment["id"]
    assert "Private PDF evidence" not in json.dumps(snapshot["attachments"])
    payload = {
        "messages": [
            {"role": "system", "content": "Use supplied evidence"},
            {"role": "tool", "tool_call_id": "read", "content": json.dumps(result)},
        ]
    }
    authorize_captured_media(state.store, row, payload["messages"])
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE topics SET deleted_at=1 WHERE id=?", (topics[0]["id"],))
    fixture = ResearchFixture("answer")
    with pytest.raises(ProviderError) as refusal:
        ResearchCompletion(state, fixture.provider, row, quote).complete(payload, timeout=5)
    assert refusal.value.no_charge and fixture.requests == []


def test_native_media_source_rechecks_in_same_transaction_as_completed_receipt(installation, monkeypatch):
    from test_public_inference import MODELS, prepare

    from reasoning_ledger.server.research_tools import ResearchTools

    _, state, _, owner, topics = prepare(installation)
    org, user, topic = owner["organization_id"], owner["user_id"], topics[1]["id"]
    attachment = upload(ResearchMedia(state.store), owner, topics[0]["id"])
    quote = replace(QUOTES[0], supports_tools=True)
    state.enqueue(user, org, topic, "read-raced-source", state.parameters([MODELS[0]], 256, 50, None), [quote])
    row = state.claim()
    original = ResearchMedia.read

    def revoke_after_read(media, *args, **kwargs):
        result = original(media, *args, **kwargs)
        with state.store.transaction(write=True) as db:
            db.execute("UPDATE topics SET deleted_at=1 WHERE id=?", (topics[0]["id"],))
        return result

    monkeypatch.setattr(ResearchMedia, "read", revoke_after_read)
    with pytest.raises(ValueError, match="tool refused: access or arguments"):
        ResearchTools(state, row).invoke(
            "media_read", {"identity": attachment["id"], "sha256": attachment["sha256"], "source_topic_id": topics[0]["id"]}
        )
    with state.store.transaction() as db:
        assert db.execute("SELECT status FROM research_tool_receipts WHERE run_id=?", (row["id"],)).fetchone()[0] == "failed"
        snapshot = json.loads(
            db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (row["request_id"],)).fetchone()[0]
        )
    assert snapshot["attachments"] == []


def test_narrow_media_projection_never_copies_other_file_description_or_unselected_receipts():
    url = "/api/organizations/" + "a" * 32 + "/topics/" + "b" * 32 + "/media/" + "c" * 32 + "/" + "d" * 64 + "/content"
    projection = hashlib.sha256(JPEG_HEADER).hexdigest()
    selected = {"url": url, "sha256": "d" * 64, "preview_sha256": projection}
    unrelated = {"url": url.replace("c" * 32, "e" * 32), "sha256": "f" * 64, "preview_sha256": projection}

    def picture(sha):
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": "Projection of original " + sha},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(JPEG_HEADER).decode()}},
            ],
        }

    snapshot = {"attachments": [selected, unrelated], "messages": [picture(unrelated["sha256"]), picture(selected["sha256"])]}
    assignment = [{"role": "system", "content": "Instructions"}, {"role": "user", "content": url}]
    messages, references = narrow_attachment_snapshot(snapshot, assignment)
    assert references == [selected]
    assert messages[0] == assignment[0] and messages[2:] == [picture(selected["sha256"]), assignment[1]]
    assert unrelated["sha256"] not in json.dumps(messages)
    assert len(snapshot["attachments"]) == 2
    messages, references = narrow_attachment_snapshot(snapshot, [{"role": "user", "content": "No attachment"}])
    assert references == [] and messages == [{"role": "user", "content": "No attachment"}]
