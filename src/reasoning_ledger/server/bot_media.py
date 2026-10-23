"""Owner-approved local TeX compilation and confined attachment publication."""

import base64
import hashlib
import os
import secrets
import shutil
from contextlib import suppress
from pathlib import Path

from .bot_client import BotClientError


def write_download(workspace, path, body, *, internal=False):
    """Publish verified bytes without exposing a partial file or replacing an existing entry."""
    from .bot_gateway import workspace_file, workspace_open

    workspace_file(workspace, path, write=not internal)
    parts = Path(path).parts
    if not parts:
        raise BotClientError("choose a relative workspace filename, not a directory")
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary, descriptor = ".reasoning-media-" + secrets.token_hex(16), None
    try:
        for part in parts[:-1]:
            with suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=directory)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise BotClientError("attachment workspace write was incomplete")
            remaining = remaining[written:]
        os.fsync(descriptor)
        identity = os.fstat(descriptor)
        current = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino) or current.st_nlink != 1:
            raise BotClientError("attachment staging file changed before publication")
        try:
            os.link(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            # Replaying an exact verified copy is safe; never overwrite a different file, link or symlink.
            with workspace_open(workspace, path, maximum=8 * 1024 * 1024) as fd, os.fdopen(fd, "rb", closefd=False) as file:
                existing = file.read(8 * 1024 * 1024 + 1)
            if len(existing) != len(body) or hashlib.sha256(existing).digest() != hashlib.sha256(body).digest():
                raise BotClientError("attachment destination exists with different bytes; choose a new workspace path") from None
        os.fsync(directory)
    finally:
        if descriptor is not None:
            identity = os.fstat(descriptor)
            try:
                current = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(descriptor)
        os.close(directory)


def register_fetch(gateway, registry):
    from .bot_gateway import workspace_file, workspace_open
    from .research_media import CHUNK_BYTES, MAX_BYTES, bounded_image

    properties = {
        "identity": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
        "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "source_topic_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
        "path": {"type": "string", "minLength": 1, "maxLength": 200},
    }

    def chunk(identity, sha256, index=0, source_topic_id=None):
        arguments = {"identity": identity, "sha256": sha256, "index": index}
        if source_topic_id:
            arguments["source_topic_id"] = source_topic_id
        return registry.invoke("media_download_chunk", arguments)

    @registry.tool(
        name="workspace_fetch_media",
        description="Fetch an exact SHA-256 attachment into a relative topic workspace path. "
        "Every bounded chunk rechecks the active lease and owner/requester/source-topic grants. "
        "Verify the full hash before atomic publication; no URLs, host paths, symlinks, hardlinks or overwrites.",
        risk="write",
        input_schema={
            "type": "object",
            "properties": properties,
            "required": ["identity", "sha256", "path"],
            "additionalProperties": False,
        },
    )
    def fetch(identity, sha256, path, source_topic_id=None):
        workspace_file(gateway.workspace, path, write=True)
        first = chunk(identity, sha256, source_topic_id=source_topic_id)
        size = first.get("size_bytes")
        if type(size) is not int or not 1 <= size <= MAX_BYTES or first.get("sha256") != sha256:
            raise BotClientError("attachment download metadata differs from its exact requested identity")
        try:
            with workspace_open(gateway.workspace, path, maximum=MAX_BYTES) as fd, os.fdopen(fd, "rb", closefd=False) as file:
                existing = file.read(MAX_BYTES + 1)
            if len(existing) != size or hashlib.sha256(existing).hexdigest() != sha256:
                raise BotClientError("attachment destination exists with different bytes; choose a new workspace path")
            return {
                key: value
                for key, value in {**first, "path": path, "source_topic_id": source_topic_id or first["topic_id"]}.items()
                if key not in {"data_base64", "chunk_sha256", "chunk_bytes", "index", "next_index"}
            }
        except FileNotFoundError:
            pass
        body = bytearray()
        for index in range((size + CHUNK_BYTES - 1) // CHUNK_BYTES):
            part = first if index == 0 else chunk(identity, sha256, index, source_topic_id)
            if (
                part.get("id") != identity
                or part.get("sha256") != sha256
                or part.get("size_bytes") != size
                or part.get("index") != index
                or part.get("mime") != first.get("mime")
            ):
                raise BotClientError("attachment metadata changed during its bounded download")
            try:
                data = base64.b64decode(part.get("data_base64", ""), validate=True)
            except ValueError:
                raise BotClientError("attachment download returned invalid base64") from None
            if len(data) != min(CHUNK_BYTES, size - index * CHUNK_BYTES) or hashlib.sha256(data).hexdigest() != part.get("chunk_sha256"):
                raise BotClientError("attachment download chunk hash differs")
            body.extend(data)
        if len(body) != size or hashlib.sha256(body).hexdigest() != sha256:
            raise BotClientError("attachment download whole-file hash differs; no workspace file was published")
        # A final authorized metadata read fences revocation between the last chunk and local publication.
        chunk(identity, sha256, source_topic_id=source_topic_id)
        write_download(gateway.workspace, path, body)
        return {
            key: value
            for key, value in {**first, "path": path, "source_topic_id": source_topic_id or first["topic_id"]}.items()
            if key not in {"data_base64", "chunk_sha256", "chunk_bytes", "index", "next_index"}
        }

    @registry.tool(
        name="workspace_view_image",
        description="Inspect an authorized, hash-matching fetched PNG/JPEG/WebP through MCP image content. "
        "Recheck live attachment access before reading the confined workspace file. SVG/PDF are not image inputs; "
        "model/image support depends on the connected CLI.",
        input_schema={
            "type": "object",
            "properties": properties,
            "required": ["identity", "sha256", "path"],
            "additionalProperties": False,
        },
    )
    def inspect(identity, sha256, path, source_topic_id=None):
        metadata = chunk(identity, sha256, source_topic_id=source_topic_id)
        mime = metadata.get("mime")
        if mime not in {"image/png", "image/jpeg", "image/webp"}:
            raise BotClientError("image inspection accepts only authorized PNG, JPEG or WebP, not SVG/PDF")
        with workspace_open(gateway.workspace, path, maximum=MAX_BYTES) as fd, os.fdopen(fd, "rb", closefd=False) as file:
            body = file.read(MAX_BYTES + 1)
        if not 1 <= len(body) <= MAX_BYTES or hashlib.sha256(body).hexdigest() != sha256 or not bounded_image(body, mime):
            raise BotClientError("workspace image differs from its authorized hash or allocation allowance")
        return {
            "path": path,
            "identity": identity,
            "sha256": sha256,
            "mime": mime,
            "size_bytes": len(body),
            "image_base64": base64.b64encode(body).decode(),
            "delivery": "MCP image; inspection requires CLI image support",
        }


def register_bot_media(gateway, registry, execute=None):
    from .bot_gateway import workspace_open

    available = {tool.name for tool in registry.list()}
    if "artifact_write" in available:
        @registry.tool(
            name="workspace_publish_text", risk="write",
            description="Publish a private workspace .tex or text file as a persistent project research file, "
                        "up to 30 KiB. Return its exact revision link to the user; overwrites need the current SHA-256.",
            input_schema={"type": "object", "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 200},
                "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            }, "required": ["path"], "additionalProperties": False},
        )
        def publish_text(path, expected_sha256=None):
            with workspace_open(gateway.workspace, path) as descriptor, os.fdopen(descriptor, "rb", closefd=False) as stream:
                body = stream.read(30 * 1024 + 1)
            if len(body) > 30 * 1024:
                raise BotClientError("publish a text source of at most 30 KiB or use the shared versioned workspace tools")
            try:
                text = body.decode("utf8")
            except UnicodeError:
                raise BotClientError("publish_text requires UTF-8 text; use workspace_publish_media for a PDF") from None
            arguments = {"path": path, "text": text}
            if expected_sha256 is not None:
                arguments["expected_sha256"] = expected_sha256
            return registry.invoke("artifact_write", arguments)
    if "media_download_chunk" in available:
        register_fetch(gateway, registry)
    if not {"media_begin", "media_chunk", "media_finish"} <= available:
        return
    path_schema = {"type": "string", "minLength": 1, "maxLength": 200}

    @registry.tool(
        name="workspace_publish_media",
        description="Publish a PNG/JPEG/WebP/PDF from this private topic workspace. "
        "No arbitrary host paths or symlinks; returns a private, checksum-pinned Markdown attachment.",
        risk="write",
        input_schema={
            "type": "object",
            "properties": {
                "path": path_schema,
                "mime": {"enum": ["image/png", "image/jpeg", "image/webp", "application/pdf"]},
                "name": path_schema,
            },
            "required": ["path", "mime"],
            "additionalProperties": False,
        },
    )
    def publish(path, mime, name=None):
        with workspace_open(gateway.workspace, path, maximum=8 * 1024 * 1024) as fd, os.fdopen(fd, "rb", closefd=False) as file:
            body = file.read(8 * 1024 * 1024 + 1)
        if not body or len(body) > 8 * 1024 * 1024:
            raise BotClientError("local media publication is limited to 8 MiB per owned workspace file")
        upload = registry.invoke(
            "media_begin",
            {"name": name or Path(path).name, "mime": mime, "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()},
        )
        for offset in range(0, len(body), upload["chunk_bytes"]):
            registry.invoke(
                "media_chunk",
                {
                    "identity": upload["id"],
                    "index": offset // upload["chunk_bytes"],
                    "data_base64": base64.b64encode(body[offset : offset + upload["chunk_bytes"]]).decode(),
                },
            )
        return registry.invoke("media_finish", {"identity": upload["id"]})

    if execute is None:
        return

    @registry.tool(
        name="tex_compile",
        description="Compile a saved .tex file to PDF using installed pdflatex, no shell escape/network, "
        "inside the isolated topic workspace after the bot owner's exact-command approval. Publish the PDF as a private attachment. "
        "Requires Linux bubblewrap and pdflatex installed in /usr or /bin; missing dependencies are reported, not installed automatically.",
        risk="write",
        idempotent=False,
        input_schema={"type": "object", "properties": {"path": path_schema}, "required": ["path"], "additionalProperties": False},
    )
    def compile_tex(path):
        if not path.endswith(".tex"):
            raise BotClientError("tex_compile requires a saved .tex source file")
        # Validate the exact source through the same descriptor-relative confinement as workspace reads.
        with workspace_open(gateway.workspace, path) as fd, os.fdopen(fd, "rb", closefd=False) as file:
            source = file.read(128 * 1024)
            source_sha256 = hashlib.sha256(source).hexdigest()
        executable = shutil.which("pdflatex")
        if not executable or not str(Path(executable).resolve()).startswith(("/usr/", "/bin/")):
            return {"refused": "Install a system TeX distribution providing pdflatex on the bot host; no server-side TeX execution occurs."}
        suffix = secrets.token_hex(16)
        source_directory = ".reasoning-tex-source-" + source_sha256 + "-" + suffix
        output_directory = ".reasoning-tex-output-" + suffix
        directory = os.open(gateway.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        source_fd = None
        try:
            os.mkdir(source_directory, 0o700, dir_fd=directory)
            os.mkdir(output_directory, 0o700, dir_fd=directory)
            write_download(gateway.workspace, source_directory + "/main.tex", source, internal=True)
            source_fd = os.open(source_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            file_fd = os.open("main.tex", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
            try:
                os.fchmod(file_fd, 0o400)
            finally:
                os.close(file_fd)
            os.fchmod(source_fd, 0o500)
        finally:
            if source_fd is not None:
                os.close(source_fd)
            os.close(directory)
        result = execute(
            [
                executable,
                "-no-shell-escape",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-output-directory=/workspace/" + output_directory,
                "./" + source_directory + "/main.tex",
            ]
        )
        if result.get("refused") or result.get("exit_code") != 0 or result.get("timed_out") or result.get("truncated"):
            return result
        with workspace_open(gateway.workspace, source_directory + "/main.tex") as fd, os.fdopen(fd, "rb", closefd=False) as file:
            if hashlib.sha256(file.read(128 * 1024)).hexdigest() != source_sha256:
                return {"refused": "Staged TeX source changed during approval or compilation; the output was not published."}
        pdf = output_directory + "/main.pdf"
        try:
            with workspace_open(gateway.workspace, pdf, maximum=8 * 1024 * 1024):
                pass
        except (FileNotFoundError, BotClientError):
            return {
                **result,
                "refused": "TeX produced no new bounded regular PDF in this fresh compilation directory; no old PDF was published.",
            }
        attachment = publish(pdf, "application/pdf", Path(path).stem + ".pdf")
        return {
            **result,
            "source_path": path,
            "source_sha256": source_sha256,
            "staged_source_path": source_directory + "/main.tex",
            "attachment": attachment,
            "inspection": "Compiled the hash-pinned primary source once. Included workspace files remain live dependencies; "
            "inspect warnings and the resulting PDF. Compilation does not verify mathematical claims.",
        }
