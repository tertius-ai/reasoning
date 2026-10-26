"""A model reply's recorded steps, described in short phrases for its timeline.

Descriptions come from the arguments and results of the reply's own tool events: a search query, an arXiv
identifier or title, a project file path, a URL's host, or a result count. They are bounded, and no tool output
text beyond a title is passed on.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

PHASES = ("round", "tool_requested", "tool_completed", "compacting", "compaction_failed", "compaction_skipped",
          "answer_ready", "reasoning_summary", "delegated", "additions_read")
SOURCES = {"arxiv": "arXiv", "crossref": "Crossref", "web": "the web", "messages": "messages", "tool": "tools",
           "knowledge": "knowledge", "ledger": "the ledger", "topics": "topics", "references": "references",
           "blog": "the blog", "media": "attachments", "account": "the account"}
LISTS = ("results", "entries", "items", "papers", "messages", "records", "matches", "tools")


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


def requested(body: dict) -> str:
    name, arguments = _call(body)
    first = lambda *keys: next((text for key in keys if (text := _text(arguments.get(key), 60))), None)  # noqa: E731
    if name == "tool_search" or name.endswith("_search"):
        source = name.removesuffix("_search")
        query = first("query", "q", "search", "terms", "text")
        return f"searched {SOURCES.get(source, source.replace('_', ' '))}" + (f" for “{query}”" if query else "")
    if name == "web_fetch" and isinstance(arguments.get("url"), str):
        host = urlsplit(arguments["url"]).hostname
        return f"fetched {host}" if host else "fetched a page"
    if name.startswith("arxiv_"):
        identifier = first("id", "arxiv_id", "identifier", "paper_id")
        return f"read arXiv {identifier}" if identifier else "read arXiv"
    if name in {"research_file_read", "artifact_read", "artifact_write"}:
        path = first("path")
        verb = "saved" if name == "artifact_write" else "read"
        return f"{verb} {path}" if path else f"{verb} a project file"
    if name == "topic_history":
        return "read earlier messages"
    if name.startswith("ledger_"):
        reading = any(word in name for word in ("read", "search", "list", "status", "graph"))
        return "read the ledger" if reading else "updated the ledger"
    if name in {"compute_submit", "noether_compute"}:
        return "submitted a computation"
    if name.startswith("lean_"):
        return "checked Lean"
    return name.replace("_", " ")


def completed(body: dict, asked: str | None) -> str:
    """The step a finished call made: what it asked, with a count of results or the title it read."""
    if body.get("ok") is False:
        return (asked or requested(body)) + " · failed"
    result = body.get("result")
    if isinstance(result, dict) and "receipt_id" in result and "result" in result:
        result = result["result"]
    title = _text(result.get("title")) if isinstance(result, dict) else None
    if title and (asked or "").startswith(("read", "fetched")):
        return f"read “{title}”"
    found = result if isinstance(result, list) else next(
        (result[key] for key in LISTS if isinstance(result, dict) and isinstance(result.get(key), list)), None)
    suffix = f" · {len(found)} result{'' if len(found) == 1 else 's'}" if isinstance(found, list) else ""
    return (asked or requested(body)) + suffix


def run_activity(db, org: str, run_id: str, limit: int = 40) -> list[dict]:
    """The reply's latest steps, oldest first, with a description for each tool call."""
    rows = db.execute(
        "SELECT sequence,event_json FROM llm_events WHERE organization_id=? AND run_id=? AND json_extract(event_json,'$.phase') IN ("
        + ",".join("?" for _ in PHASES) + ") ORDER BY sequence DESC LIMIT ?",
        (org, run_id, *PHASES, limit),
    ).fetchall()
    activity, asked = [], {}
    for row in reversed(rows):
        body = json.loads(row["event_json"])
        phase = body.get("phase")
        if phase not in PHASES:
            continue
        entry = {"sequence": row["sequence"], "phase": phase, "tool": body.get("name"), "round": body.get("round"),
                 "summary": body.get("summary")}
        if phase == "tool_requested":
            asked[body.get("tool_call_id")] = entry["detail"] = requested(body)
        elif phase == "tool_completed":
            entry["detail"] = completed(body, asked.get(body.get("tool_call_id")))
        elif phase == "additions_read":
            entry.update(message_ids=body.get("message_ids", []), step=body.get("step"))
        activity.append(entry)
    return activity
