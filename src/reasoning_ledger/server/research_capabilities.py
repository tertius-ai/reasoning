"""Effective invocation facts, supplied independently of model training cutoffs."""

import datetime
import json
import os


def runtime_capabilities(quote, metadata, tools):
    available = bool(quote.supports_tools and metadata is not None and not metadata.get("workflow_context_sha256"))
    names = sorted(tool.name for tool in tools.registry.list()) if available else []
    return {
        "current_date": datetime.datetime.now(datetime.UTC).date().isoformat(),
        "mode": "research" if available else
            "plain_comparison" if not metadata or metadata.get("workflow_context_sha256") else "plain_response",
        "model_supports_tools": quote.supports_tools,
        "advertised_tools": [tool.name for tool in tools.available()] if available else [],
        "discoverable_tools": names,
        "public_source_reader": "web_fetch" in names,
        "literature_reader": "arxiv_read" in names,
        "web_search": "web_search" in names,
        "attachments": "captured text and supported image projections; original PDFs require a granted download tool",
    }


def runtime_guidance(capabilities):
    return "\nInvocation facts supplied by the server (current UTC date and effective grants):\n" + json.dumps(capabilities) + \
        "\nUse only these capabilities. Tool discovery does not add grants. If source readers are absent, " \
        "say so and suggest a beta research topic with a tool-capable model. Do not call a past date future."


def source_availability():
    return {"web_search_configured": bool(os.environ.get("REASONING_BRAVE_API_KEY")),
            "current_date": datetime.datetime.now(datetime.UTC).date().isoformat()}
