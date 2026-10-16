"""Bounded primary-source fallback when official arXiv HTML is unavailable.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from dataclasses import replace

from ..agents.scientific import ScientificToolError
from .research_network import SourceRefused

UNAVAILABLE = (ScientificToolError, SourceRefused, OSError, TimeoutError)


def _reason(error):
    if isinstance(error, (ScientificToolError, SourceRefused)):
        return str(error).rstrip(".")
    return "the connection failed"


def register(registry):
    original = registry.get("arxiv_read")

    def read(arxiv_id, queries, context_characters=1800):
        try:
            return original.handler(arxiv_id=arxiv_id, queries=queries, context_characters=context_characters)
        except UNAVAILABLE as error:
            html = _reason(error)
        try:
            listing = registry.get("arxiv_source_list").handler(arxiv_id=arxiv_id)
        except UNAVAILABLE as error:
            raise SourceRefused(
                "arxiv_unavailable",
                f"arXiv HTML for {arxiv_id} is unavailable ({html}) and its source could not be read ({_reason(error)}).",
                "Check the identifier and version with arxiv_lookup, or read another version.",
            ) from None
        files = [item for item in listing["data"]["files"] if item["path"].endswith(".tex") and item.get("text_readable")]
        files.sort(key=lambda item: (-item.get("bytes", 0), item["path"]))
        if not files:
            raise SourceRefused(
                "arxiv_unavailable", f"arXiv HTML for {arxiv_id} is unavailable ({html}) and its source has no UTF-8 .tex file.",
                "Use arxiv_source_list to inspect the source files.",
            )
        result = registry.get("arxiv_source_read").handler(arxiv_id=arxiv_id, path=files[0]["path"],
            queries=queries, context_characters=context_characters)
        result["fallback"] = {"requested": "official_html", "used": "primary_tex", "path": files[0]["path"],
                              "reason": html,
                              "coverage": "Selected source member only; use source_list/read for other files."}
        return result

    # Register into the owning registry, without changing public tool schemas.
    registry._tools[original.name] = replace(original, handler=read)
