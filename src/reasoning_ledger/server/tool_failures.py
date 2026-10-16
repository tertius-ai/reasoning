"""Model-readable tool failure reasons: what was refused, why, and what to do instead.

Only messages authored by this application or describing the model's own
arguments are exposed. Raw upstream bodies and unexpected exception text are not.
"""

from __future__ import annotations

import re

from jsonschema import ValidationError

from ..agents.scientific import ScientificToolError
from ..agents.tools import ToolRegistryError
from .research_network import SourceRefused
from .store import StoreError

_UNPRINTABLE = re.compile(r"[\x00-\x1f\x7f]+")


def bounded(value: object, maximum: int = 300) -> str:
    text = " ".join(_UNPRINTABLE.sub(" ", str(value)).split()).rstrip(". ")
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


def tool_failure(exc: BaseException, name: str | None = None) -> tuple[str, str]:
    """Return a stable code and an actionable message for a failed tool call."""
    tool = bounded(name or "this tool", 128)
    if isinstance(exc, SourceRefused):
        return exc.code, exc.explanation()
    if isinstance(exc, ValidationError):
        path = "/".join(str(part) for part in exc.absolute_path) or "the top level"
        return "tool_arguments_invalid", (
            f"tool refused: arguments for {tool} are invalid at {bounded(path, 100)}: {bounded(exc.message)}. "
            "Use tool_search to load the exact input schema, then call again."
        )
    if isinstance(exc, ToolRegistryError) and str(exc).startswith("unknown tool: "):
        missing = bounded(str(exc).removeprefix("unknown tool: "), 128)
        return "tool_not_granted", (
            f"tool refused: {missing} is not a granted tool in this topic. Use tool_search to list granted tools; "
            "a member can enable other tool groups in Research settings."
        )
    if isinstance(exc, ScientificToolError):
        if exc.status == 429:
            return "source_rate_limited", (
                f"{bounded(exc)} after bounded retries. Wait at least a minute before another request to this source."
            )
        if exc.status == 404:
            return "source_not_found", (
                f"{bounded(exc)}. Check the identifier and its version; arxiv_lookup or a search tool can find it."
            )
        return "source_unreadable", f"{bounded(exc)}. Retry later or use an alternate source."
    if isinstance(exc, (OSError, TimeoutError)):
        return "source_connection_failed", "Public source connection failed; retry this read or use an alternate source."
    if isinstance(exc, StoreError):
        if str(exc).startswith("unknown-reference-id:"):
            return "unknown_reference_id", str(exc)
        if exc.status in {403, 404}:
            return "tool_access_refused", (
                f"tool refused: access or arguments were not accepted by {tool}; the item was not found or is "
                "outside this topic's current grants."
            )
        return "tool_request_refused", f"tool refused: {tool}: {bounded(exc)}."
    if isinstance(exc, ValueError):
        return "tool_arguments_or_content_invalid", (
            f"tool refused: {tool} received invalid arguments or unreadable source content: {bounded(exc)}."
        )
    return "tool_unavailable", f"tool failed: {tool} raised {type(exc).__name__}; retry once or use an alternate source."
