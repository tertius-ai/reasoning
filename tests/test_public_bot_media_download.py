"""Scoped original-byte retrieval and MCP image delivery never grant host paths."""

import asyncio
import base64
import hashlib
import os
from types import SimpleNamespace

import pytest
from test_public_bot_runtime import setup
from test_public_bots import fixture as bot_fixture
from test_public_bots import member
from test_public_media import PNG

from reasoning_ledger.agents.tools import ToolDefinition, ToolRegistry, build_mcp_bridge
from reasoning_ledger.server.bot_client import BotClientError
from reasoning_ledger.server.bot_media import register_bot_media
from reasoning_ledger.server.bot_tools import BotTools
from reasoning_ledger.server.research_media import ResearchMedia
from reasoning_ledger.server.store import StoreError


def reader_registry(workspace, body=PNG, mime="image/png", corrupt=False):
    registry, calls = ToolRegistry(), []
    sha = hashlib.sha256(body).hexdigest()

    def chunk(identity, sha256, index=0, source_topic_id=None):
        calls.append(index)
        data = body[index * 32768 : (index + 1) * 32768]
        return {
            "id": identity,
            "sha256": sha256,
            "mime": mime,
            "topic_id": "b" * 32,
            "size_bytes": len(body),
            "index": index,
            "data_base64": base64.b64encode(data).decode(),
            "chunk_sha256": "0" * 64 if corrupt else hashlib.sha256(data).hexdigest(),
        }

    registry.register(ToolDefinition("media_download_chunk", "read", {"type": "object"}, chunk))
    register_bot_media(SimpleNamespace(workspace=workspace), registry)
    return registry, sha, calls


def test_hash_verified_download_is_atomic_private_and_idempotent(tmp_path):
    tools, sha, calls = reader_registry(tmp_path)
    inputs = {"identity": "a" * 32, "sha256": sha, "path": "attachments/plot.png"}
    result = tools.invoke("workspace_fetch_media", inputs)
    target = tmp_path / inputs["path"]
    assert target.read_bytes() == PNG and result["sha256"] == sha
    assert target.stat().st_mode & 0o077 == 0 and target.stat().st_nlink == 1
    assert not list(target.parent.glob(".reasoning-media-*"))
    assert tools.invoke("workspace_fetch_media", inputs) == result
    assert calls == [0, 0, 0]  # Actual initial read, final lease check, replay authorization (no refetch).
    image = tools.invoke("workspace_view_image", inputs)
    assert base64.b64decode(image["image_base64"]) == PNG
    bridge = build_mcp_bridge(tools)
    blocks, manifest = asyncio.run(bridge.reasoning_call_tool("workspace_view_image", inputs))
    assert blocks[1].type == "image" and blocks[1].mimeType == "image/png" and blocks[1].data == image["image_base64"]
    assert "image_base64" not in manifest and "image_base64" not in blocks[0].text


def test_confined_download_refuses_paths_links_changed_bytes_and_bad_hash_before_publication(tmp_path):
    tools, sha, _ = reader_registry(tmp_path)

    def fetch(path):
        return tools.invoke("workspace_fetch_media", {"identity": "a" * 32, "sha256": sha, "path": path})

    for path in ["/etc/passwd", "../outside.png", "https://example.org/image.png"]:
        with pytest.raises(BotClientError):
            fetch(path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    (tmp_path / "symlink.png").symlink_to(outside)
    os.link(outside, tmp_path / "hardlink.png")
    for path in ["symlink.png", "hardlink.png"]:
        with pytest.raises(BotClientError):
            fetch(path)
    assert outside.read_bytes() == b"unchanged"
    bad, sha, _ = reader_registry(tmp_path, corrupt=True)
    with pytest.raises(BotClientError, match="chunk hash"):
        bad.invoke("workspace_fetch_media", {"identity": "a" * 32, "sha256": sha, "path": "bad.png"})
    assert not (tmp_path / "bad.png").exists() and not list(tmp_path.glob(".reasoning-media-*"))


def test_view_rechecks_source_and_refuses_pdf_svg_or_changed_workspace_image(tmp_path):
    tools, sha, _ = reader_registry(tmp_path)
    (tmp_path / "plot.png").write_bytes(b"different")
    with pytest.raises(BotClientError, match="authorized hash"):
        tools.invoke("workspace_view_image", {"identity": "a" * 32, "sha256": sha, "path": "plot.png"})
    for mime in ["application/pdf", "image/svg+xml"]:
        unsupported, sha, _ = reader_registry(tmp_path, mime=mime)
        with pytest.raises(BotClientError, match="not SVG/PDF"):
            unsupported.invoke("workspace_view_image", {"identity": "a" * 32, "sha256": sha, "path": "plot.png"})


def test_readonly_leased_bot_can_fetch_every_chunk_but_cannot_upload_and_revocation_blocks_reads(tmp_path):
    fixture = bot_fixture.__wrapped__(tmp_path)
    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    claim = runtime.claim(bot["token"])
    media, org, user = ResearchMedia(store), owner["organization_id"], owner["user_id"]
    body = b"%PDF-1.4\n" + b" " * (8 * 1024 * 1024 - 15) + b"%%EOF\n"
    start = media.begin(user, org, topics[1], "large.pdf", "application/pdf", len(body), hashlib.sha256(body).hexdigest())
    for index, offset in enumerate(range(0, len(body), 32768)):
        media.chunk(user, org, topics[1], start["id"], index, base64.b64encode(body[offset : offset + 32768]).decode())
    file = media.finish(user, org, topics[1], start["id"])
    with store.transaction(write=True) as db:
        db.execute('UPDATE bots SET scopes_json=\'["messages:read","messages:write","knowledge:read"]\' WHERE id=?', (bot["id"],))
    tools = BotTools(runtime)

    def call(name, arguments):
        return tools.invoke(bot["token"], run["id"], claim["lease"], name, arguments, "read")

    definitions = {tool["name"] for tool in tools.context(bot["token"], run["id"], claim["lease"])["tools"]}
    assert "media_download_chunk" in definitions and "media_begin" not in definitions
    downloaded = bytearray()
    for index in range(256):
        part = call("media_download_chunk", {"identity": file["id"], "sha256": file["sha256"], "index": index})
        downloaded.extend(base64.b64decode(part["data_base64"]))
    assert len(downloaded) == len(body) and hashlib.sha256(downloaded).hexdigest() == file["sha256"]
    with store.transaction(write=True) as db:
        db.execute("UPDATE bots SET revoked_at=1 WHERE id=?", (bot["id"],))
    with pytest.raises(StoreError):
        call("media_download_chunk", {"identity": file["id"], "sha256": file["sha256"], "index": 0})


def test_source_topic_needs_explicit_bot_grant_and_requester_as_well_as_owner_access(tmp_path):
    from test_public_media import upload

    from reasoning_ledger.server.bot_runtime import BotRuntime
    from reasoning_ledger.server.bot_sharing import BotSharing

    fixture = bot_fixture.__wrapped__(tmp_path)
    store, owner, topics, _, _, bots = fixture
    org, user = owner["organization_id"], owner["user_id"]
    explicit = bots.create(user, org, "Scoped reader", [topics[1]], ["messages:read", "messages:write", "knowledge:read"])
    runtime = BotRuntime(bots)
    runtime.advertise(explicit["token"], "codex", ["default"], False, False)
    run = runtime.enqueue(user, org, topics[1], f"bot/{explicit['id']}/default", "explicit-source", prompt="Read a picture")
    claim = runtime.claim(explicit["token"])
    source = upload(ResearchMedia(store), owner, topics[0])
    with pytest.raises(StoreError):
        BotTools(runtime).invoke(
            explicit["token"],
            run["id"],
            claim["lease"],
            "media_download_chunk",
            {"identity": source["id"], "sha256": source["sha256"], "source_topic_id": topics[0]},
            "read",
        )
    # A personal bot owner's access cannot silently become a different requester's private-project grant.
    other = member(fixture)
    project = store.create_entity(user, org, "projects", "Private", visibility="private", owner_id=user)
    channel = store.create_entity(user, org, "channels", "secret", project["id"])
    secret = store.create_entity(user, org, "topics", "secret", channel["id"], "beta")["id"]
    private = upload(ResearchMedia(store), owner, secret)
    personal = bots.create(
        user, org, "Owner reader", [topics[1], secret], ["messages:read", "messages:write", "knowledge:read"], personal=True
    )
    runtime.advertise(personal["token"], "codex", ["default"], False, False)
    BotSharing(bots).configure(user, org, personal["id"], "topic_writers")
    requested = runtime.enqueue(other, org, topics[1], f"bot/{personal['id']}/default", "private-request", prompt="Read a picture")
    claimed = runtime.claim(personal["token"])
    with pytest.raises(StoreError):
        BotTools(runtime).invoke(
            personal["token"],
            requested["id"],
            claimed["lease"],
            "media_download_chunk",
            {"identity": private["id"], "sha256": private["sha256"], "source_topic_id": secret},
            "read",
        )
