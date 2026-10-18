"""One validated tool registry for native calls and MCP-based executors."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from .contracts import PreparedTool

_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
TOOL_RISKS = ("read", "write", "external", "destructive")
_MCP_TOOL_RESULT_MAX_CHARACTERS = 16000
ARTIFACT_READ_CHUNK_MAX_CHARACTERS = 12000


class ToolRegistryError(ValueError):
    """A tool definition, grant, or invocation is invalid."""


# Canonical JSON of schemas that already passed the metaschema check. Tool
# registries are rebuilt for every run with the same schemas; a schema whose
# exact content was validated once is not revalidated. Failures are not cached.
_CHECKED_SCHEMAS: set[str] = set()
_CHECKED_LOCK = threading.Lock()
_CHECKED_LIMIT = 4096


def check_schema(schema: dict[str, Any]) -> None:
    try:
        identity = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        Draft202012Validator.check_schema(schema)
        return
    with _CHECKED_LOCK:
        if identity in _CHECKED_SCHEMAS:
            return
    Draft202012Validator.check_schema(schema)
    with _CHECKED_LOCK:
        if len(_CHECKED_SCHEMAS) >= _CHECKED_LIMIT:
            _CHECKED_SCHEMAS.clear()
        _CHECKED_SCHEMAS.add(identity)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]
    output_schema: dict[str, Any] | None = None
    risk: str = "read"
    capabilities: tuple[str, ...] = ()
    idempotent: bool = True

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.name):
            raise ToolRegistryError(f"invalid tool name: {self.name!r}")
        if not self.description.strip():
            raise ToolRegistryError(f"tool {self.name} needs a description")
        if self.risk not in TOOL_RISKS:
            raise ToolRegistryError(
                f"tool risk must be one of {', '.join(TOOL_RISKS)}"
            )
        check_schema(self.input_schema)
        if self.output_schema:
            check_schema(self.output_schema)


@dataclass(frozen=True)
class ToolGrant:
    tool_ids: tuple[str, ...]
    allowed_risks: tuple[str, ...] = ("read",)
    capabilities: tuple[str, ...] = ()


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> ToolDefinition:
        if definition.name in self._tools:
            raise ToolRegistryError(f"tool already registered: {definition.name}")
        self._tools[definition.name] = definition
        return definition

    def tool(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any] | None = None,
        risk: str = "read",
        capabilities: tuple[str, ...] = (),
        idempotent: bool = True,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.register(
                ToolDefinition(
                    name=name,
                    description=description,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    handler=handler,
                    risk=risk,
                    capabilities=capabilities,
                    idempotent=idempotent,
                )
            )
            return handler

        return decorate

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolRegistryError(f"unknown tool: {name}") from exc

    def list(self) -> list[ToolDefinition]:
        return [self._tools[name] for name in sorted(self._tools)]

    def select(self, grant: ToolGrant) -> list[ToolDefinition]:
        selected = []
        capabilities = set(grant.capabilities)
        allowed_risks = set(grant.allowed_risks)
        for name in grant.tool_ids:
            definition = self.get(name)
            if definition.risk not in allowed_risks:
                raise ToolRegistryError(
                    f"tool {name} has {definition.risk!r} risk, which this run does not grant"
                )
            missing = set(definition.capabilities) - capabilities
            if missing:
                raise ToolRegistryError(
                    f"tool {name} needs capabilities: {', '.join(sorted(missing))}"
                )
            selected.append(definition)
        return selected

    def prepared(self, definitions: Iterable[ToolDefinition]) -> tuple[PreparedTool, ...]:
        return tuple(
            PreparedTool(tool.name, tool.description, tool.input_schema)
            for tool in definitions
        )

    def invoke(self, name: str, arguments: Mapping[str, Any]) -> Any:
        definition = self.get(name)
        Draft202012Validator(definition.input_schema).validate(dict(arguments))
        result = definition.handler(**dict(arguments))
        if definition.output_schema:
            Draft202012Validator(definition.output_schema).validate(result)
        return result


def build_mcp_bridge(
    registry: ToolRegistry,
    *,
    allowed_tools: Iterable[str] | None = None,
    max_calls: int | None = None,
    completion_tool_calls: Mapping[str, int] | None = None,
    completion_artifacts: Iterable[str] = (),
    name: str = "reasoning-tools",
    on_list_tools: Callable[[], None] | None = None,
):
    """Expose registry definitions verbatim over a low-level MCP server."""

    try:
        from mcp.server import Server
        from mcp.types import ImageContent, TextContent, Tool, ToolAnnotations
    except ImportError as exc:  # pragma: no cover - broken installation
        raise RuntimeError("MCP support requires the mcp package") from exc

    if max_calls is not None and max_calls <= 0:
        raise ValueError("max_calls must be positive")
    allowed = set(allowed_tools) if allowed_tools is not None else None
    definitions = [definition for definition in registry.list() if allowed is None or definition.name in allowed]
    exposed = {definition.name: definition for definition in definitions}
    artifact_writers = {
        definition.name
        for definition in definitions
        if "artifacts:write" in definition.capabilities
    }
    required_calls = dict(completion_tool_calls or {})
    required_artifact_paths = set(completion_artifacts)
    if any(name not in exposed for name in required_calls):
        missing = sorted(name for name in required_calls if name not in exposed)
        raise ValueError("completion tools are not exposed by this bridge: " + ", ".join(missing))
    if required_artifact_paths and not artifact_writers:
        raise ValueError("completion artifacts require an artifacts:write tool")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in required_calls.values()):
        raise ValueError("completion tool call minimums must be positive integers")
    required_writer_calls = sum(
        value
        for tool_name, value in required_calls.items()
        if tool_name in artifact_writers
    )
    minimum_reserved_calls = sum(
        value
        for tool_name, value in required_calls.items()
        if tool_name not in artifact_writers
    ) + max(required_writer_calls, len(required_artifact_paths))
    if max_calls is not None and minimum_reserved_calls > max_calls:
        raise ValueError(f"completion contract needs at least {minimum_reserved_calls} calls but the bridge budget is {max_calls}")
    call_lock = threading.Lock()
    call_count = 0
    successful_calls: dict[str, int] = {}
    in_flight_calls: dict[str, int] = {}
    successful_artifacts: set[str] = set()
    in_flight_artifacts: set[str] = set()
    server = Server(
        name,
        instructions=(
            "Reasoning project tools. Calls are validated against the same schemas "
            "and implementations used by the Reasoning Native executor. Completion-"
            "bound runs reserve enough of the call budget for every outstanding "
            "required tool and artifact."
        ),
    )

    @server.list_tools()
    async def list_tools():
        if on_list_tools is not None:
            on_list_tools()
        return [
            Tool(
                name=definition.name,
                description=(
                    f"Canonical Reasoning tool ID: {definition.name}\n\n"
                    f"{definition.description}"
                ),
                inputSchema=definition.input_schema,
                outputSchema=definition.output_schema,
                annotations=ToolAnnotations(
                    readOnlyHint=definition.risk == "read",
                    destructiveHint=definition.risk == "destructive",
                    idempotentHint=definition.idempotent,
                    openWorldHint=definition.risk == "external",
                ),
            )
            for definition in definitions
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(tool_name: str, arguments: dict[str, Any]):
        nonlocal call_count
        reserved_tool = False
        reserved_artifact: str | None = None
        with call_lock:
            if tool_name not in exposed:
                raise ToolRegistryError(f"tool is not granted by this MCP bridge: {tool_name}")
            deficits = {
                name: minimum - successful_calls.get(name, 0) - in_flight_calls.get(name, 0)
                for name, minimum in required_calls.items()
                if minimum - successful_calls.get(name, 0) - in_flight_calls.get(name, 0) > 0
            }
            pending_artifacts = required_artifact_paths - successful_artifacts - in_flight_artifacts
            artifact_path = (
                arguments.get("path") if tool_name in artifact_writers else None
            )
            contributes_tool = deficits.get(tool_name, 0) > 0
            contributes_artifact = isinstance(artifact_path, str) and artifact_path in pending_artifacts
            write_deficit = sum(
                deficits.pop(writer, 0)
                for writer in artifact_writers
            )
            reserved_calls = sum(deficits.values()) + max(
                write_deficit,
                len(pending_artifacts),
            )
            remaining_calls = None if max_calls is None else max_calls - call_count
            if (
                reserved_calls > 0
                and remaining_calls is not None
                and remaining_calls <= reserved_calls
                and not contributes_tool
                and not contributes_artifact
            ):
                raise ToolRegistryError(
                    f"Reasoning MCP completion capacity is reserved for outstanding required calls; {tool_name} was not executed"
                )
            if max_calls is not None and call_count >= max_calls:
                raise ToolRegistryError(
                    f"Reasoning MCP tool-call budget exhausted after {max_calls} calls; this queued call was not executed"
                )
            call_count += 1
            if contributes_tool:
                in_flight_calls[tool_name] = in_flight_calls.get(tool_name, 0) + 1
                reserved_tool = True
            if contributes_artifact:
                in_flight_artifacts.add(artifact_path)
                reserved_artifact = artifact_path
        try:
            result = registry.invoke(tool_name, arguments)
        except Exception:
            with call_lock:
                if reserved_tool:
                    in_flight_calls[tool_name] -= 1
                if reserved_artifact is not None:
                    in_flight_artifacts.discard(reserved_artifact)
            raise
        with call_lock:
            if reserved_tool:
                in_flight_calls[tool_name] -= 1
                successful_calls[tool_name] = successful_calls.get(tool_name, 0) + 1
            if reserved_artifact is not None:
                in_flight_artifacts.discard(reserved_artifact)
                successful_artifacts.add(reserved_artifact)
        if tool_name == "workspace_view_image" and isinstance(result, dict):
            import base64
            import hashlib

            from ..server.research_media import MAX_BYTES, bounded_image

            mime, encoded = result.get("mime"), result.get("image_base64")
            if (mime not in {"image/png", "image/jpeg", "image/webp"} or not isinstance(encoded, str)
                    or len(encoded) > 4 * ((MAX_BYTES + 2) // 3)):
                raise ToolRegistryError("MCP image delivery requires a bounded PNG/JPEG/WebP")
            try:
                image = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise ToolRegistryError("MCP image delivery requires valid base64") from None
            if len(image) > MAX_BYTES or not bounded_image(image, mime) or hashlib.sha256(image).hexdigest() != result.get("sha256"):
                raise ToolRegistryError("MCP image delivery differs from its declared image identity")
            manifest = {key: value for key, value in result.items() if key != "image_base64"}
            return [TextContent(type="text", text=json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))),
                    ImageContent(type="image", data=encoded, mimeType=mime)], manifest
        if tool_name == "artifact_read_chunk" and isinstance(result, dict):
            metadata = {key: value for key, value in result.items() if key != "text"}
            blocks = [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "artifact_chunk": {
                                "delivery": "chunk-v1",
                                "delivery_complete": True,
                                **metadata,
                            }
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
                TextContent(type="text", text=result["text"]),
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "artifact_chunk_receipt": {
                                "delivery": "chunk-v1",
                                "delivery_complete": True,
                                "path": result["path"],
                                "start_character": result["start_character"],
                                "end_character": result["end_character"],
                                "sha256": result["sha256"],
                            }
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            ]
            return blocks, result
        if tool_name == "artifact_read" and isinstance(result, dict):
            serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            if len(serialized) > _MCP_TOOL_RESULT_MAX_CHARACTERS:
                manifest = {
                    "artifact_manifest": {
                        "delivery": "manifest-only-v1",
                        "content_complete": False,
                        "path": result["path"],
                        "bytes": result["bytes"],
                        "characters": len(result["text"]),
                        "sha256": result["sha256"],
                        "required_tool": "artifact_read_chunk",
                        "next_request": {
                            "path": result["path"],
                            "start_character": 0,
                            "max_characters": ARTIFACT_READ_CHUNK_MAX_CHARACTERS,
                        },
                    }
                }
                blocks = [
                    TextContent(
                        type="text",
                        text=json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                    )
                ]
                return blocks, manifest
        if tool_name == "artifact_read_many" and isinstance(result, dict) and isinstance(result.get("artifacts"), list):
            artifacts = result["artifacts"]
            paths = [artifact.get("path") for artifact in artifacts]
            blocks = [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "artifact_bundle": {
                                "delivery": "multi-block-v2",
                                "content_complete": True,
                                "artifact_count": len(artifacts),
                                "bytes": result.get("bytes"),
                                "paths": paths,
                            }
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            ]
            blocks.extend(
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "artifact": artifact,
                            "ordinal": ordinal,
                            "artifact_count": len(artifacts),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                for ordinal, artifact in enumerate(artifacts, start=1)
            )
            blocks.append(
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "artifact_bundle_receipt": {
                                "delivery": "multi-block-v2",
                                "content_complete": True,
                                "complete": True,
                                "artifact_count": len(artifacts),
                                "paths": paths,
                            }
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            )
            if sum(len(block.text) for block in blocks) <= _MCP_TOOL_RESULT_MAX_CHARACTERS:
                return blocks, result

            manifest = {
                "artifact_bundle_manifest": {
                    "delivery": "manifest-only-v1",
                    "content_complete": False,
                    "artifact_count": len(artifacts),
                    "bytes": result.get("bytes"),
                    "artifacts": [
                        {
                            "ordinal": ordinal,
                            "path": artifact["path"],
                            "bytes": artifact["bytes"],
                            "characters": len(artifact["text"]),
                            "sha256": artifact["sha256"],
                            "next_request": {
                                "path": artifact["path"],
                                "start_character": 0,
                                "max_characters": ARTIFACT_READ_CHUNK_MAX_CHARACTERS,
                            },
                        }
                        for ordinal, artifact in enumerate(artifacts, start=1)
                    ],
                    "required_tool": "artifact_read_chunk",
                    "instruction": (
                        "No artifact body was delivered. Read every required path with "
                        "artifact_read_chunk and follow next_start_character until complete."
                    ),
                }
            }
            manifest_text = json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(manifest_text) > _MCP_TOOL_RESULT_MAX_CHARACTERS:
                manifest_text = json.dumps(
                    {
                        "artifact_bundle_manifest": {
                            "delivery": "manifest-only-v1",
                            "content_complete": False,
                            "artifact_count": len(artifacts),
                            "bytes": result.get("bytes"),
                            "artifact_metadata_omitted": True,
                            "requested_paths_known_to_client": True,
                            "required_tool": "artifact_read_chunk",
                            "instruction": (
                                "No artifact body was delivered. Use each requested input "
                                "path with artifact_read_chunk from start_character zero and "
                                "follow next_start_character until complete."
                            ),
                        }
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            return [TextContent(type="text", text=manifest_text)], json.loads(manifest_text)
        blocks = [
            TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            )
        ]
        return (blocks, result) if isinstance(result, dict) else blocks

    # Keep direct helpers available for in-process verification and embedding;
    # Server.list_tools/call_tool are decorator factories in the low-level SDK.
    server.reasoning_list_tools = list_tools
    server.reasoning_call_tool = call_tool
    return server


def serve_mcp_bridge(
    registry: ToolRegistry,
    *,
    allowed_tools: Iterable[str] | None = None,
    max_calls: int | None = None,
    completion_tool_calls: Mapping[str, int] | None = None,
    completion_artifacts: Iterable[str] = (),
) -> None:
    from mcp.server.stdio import stdio_server

    server = build_mcp_bridge(
        registry,
        allowed_tools=allowed_tools,
        max_calls=max_calls,
        completion_tool_calls=completion_tool_calls,
        completion_artifacts=completion_artifacts,
    )

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(run())
