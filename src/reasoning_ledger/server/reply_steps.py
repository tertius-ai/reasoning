"""A model reply's recorded steps, described in short phrases for its timeline.

Descriptions come from the arguments and results of the reply's own tool events: a search query, an arXiv
identifier or title, a project file path, a URL's host, or a result count. They are bounded, and no tool output
text beyond a title is passed on. A call reads as running ("fetching ncatlab.org") until it completes, then as
done ("fetched ncatlab.org"), failed ("couldn't fetch ncatlab.org"), or skipped when no tool calls were left.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

PHASES = ("round", "tool_requested", "tool_completed", "compacting", "compaction_failed", "compaction_skipped",
          "answer_ready", "reasoning_summary", "delegated", "additions_read", "provider_credit_wait")
SOURCES = {"arxiv": "arXiv", "crossref": "Crossref", "web": "the web", "messages": "messages", "tool": "tools",
           "knowledge": "knowledge", "ledger": "the ledger", "topics": "topics", "references": "references",
           "blog": "the blog", "media": "attachments", "account": "the account"}
LISTS = ("results", "entries", "items", "papers", "messages", "records", "matches", "tools")
# A call's verb while it runs and once it has finished, keyed by its plain form.
VERBS = {"search": ("searching", "searched"), "fetch": ("fetching", "fetched"), "read": ("reading", "read"),
         "save": ("saving", "saved"), "update": ("updating", "updated"), "submit": ("submitting", "submitted"),
         "check": ("checking", "checked"), "use": ("using", "used")}


def _text(value: object, limit: int = 80) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _call(body: dict) -> tuple[str, dict]:
    """The tool a call ran and its arguments; a dispatched `tool_call` names the tool it ran."""
    name, arguments = str(body.get("name") or ""), body.get("arguments")
    if name == "tool_call" and isinstance(arguments, dict):
        name, arguments = str(arguments.get("name") or name), arguments.get("arguments")
    return name, arguments if isinstance(arguments, dict) else {}


def _host(url: object) -> str | None:
    try:
        return urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        return None


def _action(body: dict) -> tuple[str, str]:
    """What a call does, as a plain verb and its object: ("fetch", "ncatlab.org")."""
    name, arguments = _call(body)
    first = lambda *keys: next((text for key in keys if (text := _text(arguments.get(key), 60))), None)  # noqa: E731
    if name == "tool_search" or name.endswith("_search"):
        source = name.removesuffix("_search")
        query = first("query", "q", "search", "terms", "text")
        return "search", SOURCES.get(source, source.replace("_", " ")) + (f" for “{query}”" if query else "")
    if name == "web_fetch":
        return "fetch", _host(arguments.get("url")) or "a page"
    if name.startswith("arxiv_"):
        identifier = first("id", "arxiv_id", "identifier", "paper_id")
        return "read", f"arXiv {identifier}" if identifier else "arXiv"
    if name in {"research_file_read", "artifact_read", "artifact_write"}:
        return "save" if name == "artifact_write" else "read", first("path") or "a project file"
    if name == "topic_history":
        return "read", "earlier messages"
    if name.startswith("ledger_"):
        reading = any(word in name for word in ("read", "search", "list", "status", "graph"))
        return "read" if reading else "update", "the ledger"
    if name in {"compute_submit", "noether_compute"}:
        return "submit", "a computation"
    if name.startswith("lean_"):
        return "check", "Lean"
    return "use", name.replace("_", " ")


def requested(body: dict) -> str:
    """The step a call is making while it runs: "fetching ncatlab.org"."""
    verb, subject = _action(body)
    return f"{VERBS[verb][0]} {subject}"


def completed(body: dict, request: dict | None = None) -> str:
    """The step a finished call made, described from its request: what it did, with a count of results or the title
    it read; that it failed; or that it never ran because the reply had no tool calls left."""
    verb, subject = _action(request or body)
    if body.get("ok") is False:
        if body.get("skipped") is True:
            return f"skipped {VERBS[verb][0]} {subject} · no tool calls left"
        return f"couldn't {verb} {subject}"
    result = body.get("result")
    if isinstance(result, dict) and "receipt_id" in result and "result" in result:
        result = result["result"]
    title = _text(result.get("title")) if isinstance(result, dict) else None
    if title and verb in {"read", "fetch"}:
        return f"read “{title}”"
    found = result if isinstance(result, list) else next(
        (result[key] for key in LISTS if isinstance(result, dict) and isinstance(result.get(key), list)), None)
    suffix = f" · {len(found)} result{'' if len(found) == 1 else 's'}" if isinstance(found, list) else ""
    return f"{VERBS[verb][1]} {subject}{suffix}"


def run_activity(db, org: str, run_id: str, limit: int = 40) -> list[dict]:
    """The reply's latest steps, oldest first, with a description for each tool call."""
    rows = db.execute(
        "SELECT sequence,event_json FROM llm_events WHERE organization_id=? AND run_id=? AND json_extract(event_json,'$.phase') IN ("
        + ",".join("?" for _ in PHASES) + ") ORDER BY sequence DESC LIMIT ?",
        (org, run_id, *PHASES, limit),
    ).fetchall()
    activity, requests = [], {}
    for row in reversed(rows):
        body = json.loads(row["event_json"])
        phase = body.get("phase")
        if phase not in PHASES:
            continue
        entry = {"sequence": row["sequence"], "phase": phase, "tool": body.get("name"), "round": body.get("round"),
                 "summary": body.get("summary")}
        if phase == "tool_requested":
            requests[body.get("tool_call_id")] = body
            entry["detail"] = requested(body)
        elif phase == "tool_completed":
            entry["detail"] = completed(body, requests.get(body.get("tool_call_id")))
        elif phase == "additions_read":
            entry.update(message_ids=body.get("message_ids", []), step=body.get("step"))
        activity.append(entry)
    return activity
