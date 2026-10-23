"""Local MCP gateway: confined files, canonical knowledge, owner-approved execution.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from ..agents.tools import ToolDefinition, ToolRegistry, build_mcp_bridge
from ..deployment.private import read_document
from .bot_client import BotClient, BotClientError
from .workbench_tools import READS


def workspace_file(workspace, name, *, write=False):
    if not isinstance(name, str) or not name or len(name) > 300:
        raise BotClientError("choose a relative workspace file")
    if "://" in name or name.startswith(("data:", "file:")):
        raise BotClientError("workspace file access does not accept URLs")
    relative = Path(name)
    if relative.is_absolute() or any(part in {"..", "."} for part in relative.parts):
        raise BotClientError("files must remain inside this topic workspace")
    if write and any(part.startswith((".reasoning-tex-source-", ".reasoning-tex-output-")) for part in relative.parts):
        raise BotClientError("compilation staging directories are reserved for the approved TeX operation")
    target = workspace / relative
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise BotClientError("workspace symlinks are not followed")
    if not target.resolve().is_relative_to(workspace.resolve()):
        raise BotClientError("file is outside this topic workspace")
    return target


@contextmanager
def workspace_open(workspace, name, *, write=False, maximum=128 * 1024):
    """Resolve every directory relative to pinned descriptors; no symlink-swap escape."""
    workspace_file(workspace, name, write=write)
    parts = Path(name).parts
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    file = None
    try:
        for part in parts[:-1]:
            if write:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=directory)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_WRONLY | os.O_CREAT if write else os.O_RDONLY)
        file = os.open(parts[-1], flags, 0o600, dir_fd=directory)
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > maximum:
            raise BotClientError("workspace access requires a bounded owned regular file without hardlinks")
        if write:
            os.ftruncate(file, 0)
        yield file
    finally:
        if file is not None:
            os.close(file)
        os.close(directory)


def sandbox_arguments(workspace, command):
    """Expose the toolchain, never the owner's home, private state, environment or network."""
    bwrap = shutil.which("bwrap")
    if not bwrap or sys.platform != "linux":
        raise BotClientError("local execution needs Linux bubblewrap (bwrap); install it or use Modal compute")
    args = [bwrap, "--die-with-parent", "--unshare-all", "--new-session", "--clearenv",
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin", "--setenv", "HOME", "/workspace",
            "--setenv", "LANG", "C.UTF-8", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for directory in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        if Path(directory).exists():
            args += ["--ro-bind", directory, directory]
    # CAS tools may use system libraries; credentials and host/user config are not mounted.
    for file in ("/etc/ld.so.cache", "/etc/alternatives", "/etc/texmf", "/var/lib/texmf", "/etc/fonts"):
        if Path(file).exists():
            args += ["--ro-bind", file, file]
    args += ["--bind", str(workspace), "/workspace"]
    if command and Path(command[0]).name == "pdflatex":
        primary = Path(command[-1])
        if len(primary.parts) == 2 and primary.parts[0].startswith(".reasoning-tex-source-"):
            source = workspace_file(workspace, primary.parts[0])
            args += ["--ro-bind", str(source), "/workspace/" + primary.parts[0]]
    return args + ["--chdir", "/workspace", "--", *command]


def bounded_execute(arguments, *, timeout=300, maximum=128 * 1024):
    """Drain a bounded pipe; an approved noisy command cannot fill the host's temporary disk."""
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env={}, start_new_session=True)
    output, truncated, timed_out = bytearray(), False, False
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                ready = selector.select(min(0.2, max(0, deadline - time.monotonic())))
                if not ready:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                remaining = maximum - len(output)
                output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
                    break
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        process.stdout.close()
    return {"exit_code": process.returncode, "output": output.decode("utf8", "replace"), "truncated": truncated, "timed_out": timed_out}


class Gateway:
    def __init__(self, document, *, client=None):
        self.client = client or BotClient(document["configuration"])
        self.run, self.lease = document["run_id"], document["lease"]
        self.workspace = Path(document["workspace"]).resolve(strict=True)
        self.local_tools = document["local_tools"]
        self.path = f"/api/bot/runs/{self.run}"
        self.receipt_directory = document.get("receipt_directory")
        self.developer_policy = document.get("developer_policy")

    def request(self, suffix, **values):
        return self.client._request("POST", self.path + suffix, {"lease": self.lease, **values})

    def registry(self):
        registry = ToolRegistry()
        context = self.request("/context")
        for tool in context["tools"]:
            if tool["name"] in {"topic_context", "tool_search", "tool_call"}:
                continue  # Discovery describes the composed local + server registry, not just server tools.
            def call(_name=tool["name"], **arguments):
                return self.request("/tools", name=_name, arguments=arguments, client_id=secrets.token_hex(16))
            readonly = READS | {"topic_context", "blog_read", "topic_history", "messages_search", "topics_search",
                                "media_list", "media_read", "media_download_chunk",
                                "artifact_list", "research_file_read"}
            registry.register(ToolDefinition(tool["name"], tool["description"], tool["input_schema"], call,
                              risk="read" if tool["name"] in readonly else "write"))
        string = {"type": "string", "minLength": 1, "maxLength": 300}

        @registry.tool(name="workspace_read", description="Read a text file in this topic's private bot workspace.",
                       input_schema={"type": "object", "properties": {"path": string}, "required": ["path"]})
        def read(path):
            self.request("/progress", phase="reading", activity="reading_workspace")
            with workspace_open(self.workspace, path) as fd, os.fdopen(fd, "r", closefd=False) as file:
                return {"path": path, "text": file.read(128 * 1024)}

        @registry.tool(name="workspace_write", description="Write a text file inside this topic's private bot workspace.", risk="write",
                       input_schema={"type": "object", "properties": {"path": string, "text": {"type": "string", "maxLength": 64000}},
                                     "required": ["path", "text"]})
        def write(path, text):
            self.request("/progress", phase="writing", activity="writing_workspace")
            with workspace_open(self.workspace, path, write=True) as fd, os.fdopen(fd, "w", closefd=False) as file:
                file.write(text)
            return {"path": path, "bytes": len(text.encode())}

        @registry.tool(name="shared_knowledge_search", description="Search this project's ledger notes, bibliography, and blog on demand.",
                       input_schema={"type": "object", "properties": {"query": {"type": "string", "maxLength": 200},
                                      "kind": {"enum": ["ledger", "bibliography", "journal"]},
                                      "scope": {"enum": ["working_set", "topic", "project"]},
                                      "before": {"type": "string", "maxLength": 100}}})
        def knowledge(query="", kind=None, scope=None, before=None):
            return self.request("/knowledge", query=query, kind=kind, scope=scope, before=before)

        execute = None
        if self.local_tools:
            @registry.tool(name="local_execute", description="Request the bot owner's permission for this exact local command. "
                           "Execution is isolated to this topic workspace with no network or home-directory access.", risk="write",
                           idempotent=False, input_schema={"type": "object", "properties": {"command": {"type": "array",
                           "minItems": 1, "maxItems": 32, "items": {"type": "string", "minLength": 1, "maxLength": 8000}}},
                           "required": ["command"], "additionalProperties": False})
            def execute(command):
                args = sandbox_arguments(self.workspace, command)  # fail closed before requesting consent
                approval = self.request("/executions", command=command)
                while approval["status"] == "pending":
                    self.request("/progress", phase="waiting_approval", activity="waiting_approval")
                    time.sleep(2)
                    approval = self.request(f"/executions/{approval['id']}/status")
                if approval["status"] != "approved":
                    return {"refused": "The bot owner did not approve this exact execution."}
                receipt = self.request(f"/executions/{approval['id']}/consume")
                if json.loads(receipt["command_json"]) != command:
                    raise BotClientError("execution consent differs from the exact requested command")
                # The consume receipt is durable before process creation. A lost outcome is never retried automatically.
                executable = Path(command[0]).name
                activity = "running_lean" if executable in {"lean", "lake"} else (
                    "compiling_tex" if executable == "pdflatex" else "running_compute")
                self.request("/progress", phase="using_tools", activity=activity)
                result = bounded_execute(args)
                self.request("/progress", phase="thinking")
                return {**result, "approval_id": approval["id"]}
        from .bot_media import register_bot_media

        register_bot_media(self, registry, execute)
        from .bot_issue_gateway import register

        register(self, registry, self.receipt_directory, context.get("developer", {"enabled": False}))
        from .bot_testing import register as register_testing

        register_testing(self, registry, self.developer_policy, context)
        self.register_discovery(registry, context)
        return registry

    def register_discovery(self, registry, initial):
        """Refresh server grants; local registration never overrides current authorization."""
        server_names = {tool["name"] for tool in initial["tools"]}
        developer_names = {"server_diagnostics", "github_issue_create", "development_source_read", "development_test"}
        repository = (read_document(Path(self.developer_policy))["repository"]
                      if any(tool.name == "development_source_read" for tool in registry.list()) else None)

        def definitions():
            current = self.request("/context")  # Checks live lease, topic access and requester permissions.
            granted = {tool["name"]: tool for tool in current["tools"]}
            result = []
            for definition in registry.list():
                if definition.name in server_names:
                    if definition.name in granted:
                        result.append(granted[definition.name])
                    continue
                if definition.name in developer_names and not current.get("developer", {}).get("enabled"):
                    continue
                if (definition.name in {"development_source_read", "development_test"}
                        and repository not in current["developer"]["repositories"]):
                    continue
                input_schema = definition.input_schema
                if definition.name == "github_issue_create":
                    properties = input_schema["properties"]
                    allowed = [repo for repo in properties["repository"]["enum"] if repo in current["developer"]["repositories"]]
                    if not allowed:
                        continue
                    input_schema = {**input_schema, "properties": {**properties,
                        "repository": {**properties["repository"], "enum": allowed}}}
                result.append({"name": definition.name, "description": definition.description,
                               "input_schema": input_schema})
            return result

        schemas = {tool["name"]: tool for tool in initial["tools"]}
        def context():
            names = [tool["name"] for tool in definitions()]
            value = self.request("/tools", name="topic_context", arguments={}, client_id=secrets.token_hex(16))
            self.request("/gateway", stage="cli", tool_count=len(names))
            return {**value, "tools": names}

        def search(query):
            words = query.lower().split()
            return [tool for tool in definitions() if tool["name"] not in {"tool_search", "tool_call"}
                    and any(word in (tool["name"] + " " + tool["description"]).lower() for word in words)][:8]

        def call(name, arguments):
            if name in {"tool_search", "tool_call"}:
                raise BotClientError("recursive tool discovery is not permitted")
            if name not in {tool["name"] for tool in definitions()}:
                raise BotClientError("this bot invocation does not currently grant that tool")
            return {"name": name, "result": registry.invoke(name, arguments)}

        for name, handler in (("topic_context", context), ("tool_search", search), ("tool_call", call)):
            if name in schemas:
                tool = schemas[name]
                registry.register(ToolDefinition(name, tool["description"], tool["input_schema"], handler,
                    risk="write" if name == "tool_call" else "read", idempotent=name != "tool_call"))


async def serve(path, *, probe=False):
    from mcp.server.stdio import stdio_server

    gateway = Gateway(read_document(path))
    registry = gateway.registry()
    server = build_mcp_bridge(registry, name="reasoning-topic",
                             on_list_tools=lambda: gateway.request("/gateway", stage="transport" if probe else "cli",
                                 tool_count=len(registry.list())))
    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        gateway.client.close()


if __name__ == "__main__":
    asyncio.run(serve(Path(sys.argv[1]), probe="--probe" in sys.argv[2:]))
