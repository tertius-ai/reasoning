"""Shared bounds for direct research-file delivery and its tool envelope."""

MAX_ARTIFACT_TEXT_BYTES = 256 * 1024
# JSON can escape one UTF-8 source byte as six ASCII bytes (for example \u0001).
# Leave 4 KiB for the path, expected checksum and the tool_call wrapper.
MAX_TOOL_ARGUMENT_BYTES = 6 * MAX_ARTIFACT_TEXT_BYTES + 4096
