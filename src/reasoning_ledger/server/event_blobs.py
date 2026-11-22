"""Provider requests kept once per distinct message and tool list, and expanded exactly for readers.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

A provider-request event (``{"type": "agent", "phase": "provider_request", "request": {...}}``) is stored with each
entry of ``request.messages`` and the whole ``request.tools`` value replaced by ``{"$blob": sha256}`` and with
``"request_encoding": ENCODING`` added. Every other field stays inline in its original order. ``expand`` restores the
exact original event. Events without ``request_encoding`` are returned unchanged, so requests recorded inline remain
readable.
"""

from __future__ import annotations

import hashlib
import json
import re

ENCODING = "sha256-blobs-v1"
DIGEST = re.compile(r"[0-9a-f]{64}")
# Bound one lookup's parameter list well below SQLite's and PostgreSQL's limits.
LOOKUP_BATCH = 200


def _blob(db, org: str, value) -> dict:
    body = json.dumps(value, ensure_ascii=False)
    digest = hashlib.sha256(body.encode()).hexdigest()
    db.execute("INSERT OR IGNORE INTO llm_event_blobs(organization_id,sha256,body) VALUES(?,?,?)", (org, digest, body))
    return {"$blob": digest}


def stored_request(db, org: str, event: dict) -> dict:
    """``event`` as it is stored: its request's messages and tools kept once each in ``llm_event_blobs``."""
    request = event.get("request")
    if not isinstance(request, dict) or "request_encoding" in event:
        return event
    packed = dict(request)
    if isinstance(packed.get("messages"), list):
        packed["messages"] = [_blob(db, org, message) for message in packed["messages"]]
    if "tools" in packed:
        packed["tools"] = _blob(db, org, packed["tools"])
    return {**event, "request": packed, "request_encoding": ENCODING}


def _reference(value) -> str:
    if not isinstance(value, dict) or set(value) != {"$blob"} or not isinstance(value["$blob"], str) \
            or not DIGEST.fullmatch(value["$blob"]):
        raise ValueError("stored provider request has an invalid blob reference")
    return value["$blob"]


def expand(db, org: str, event: dict, cache: dict | None = None) -> dict:
    """The event exactly as it was recorded. ``cache`` may be shared across one reader's events of one organization."""
    if "request_encoding" not in event:
        return event
    request = event.get("request")
    if event["request_encoding"] != ENCODING or not isinstance(request, dict):
        raise ValueError("stored provider request has an unknown encoding")
    messages = request.get("messages")
    references = [_reference(item) for item in messages] if isinstance(messages, list) else []
    if "tools" in request:
        references.append(_reference(request["tools"]))
    cache = {} if cache is None else cache
    missing = sorted({digest for digest in references if digest not in cache})
    for start in range(0, len(missing), LOOKUP_BATCH):
        batch = missing[start:start + LOOKUP_BATCH]
        for digest, body in db.execute(
            "SELECT sha256,body FROM llm_event_blobs WHERE organization_id=? AND sha256 IN (" + ",".join("?" * len(batch)) + ")",
            (org, *batch),
        ):
            cache[digest] = json.loads(body)
    if any(digest not in cache for digest in references):
        raise ValueError("stored provider request refers to a missing blob")
    restored = dict(request)
    if isinstance(messages, list):
        restored["messages"] = [cache[_reference(item)] for item in messages]
    if "tools" in request:
        restored["tools"] = cache[_reference(request["tools"])]
    return {key: restored if key == "request" else value for key, value in event.items() if key != "request_encoding"}


def compact_requests(db) -> int:
    """Store every provider request recorded inline in the blob form; returns how many events were rewritten."""
    sequences = [row[0] for row in db.execute(
        "SELECT sequence FROM llm_events WHERE json_extract(event_json,'$.phase')='provider_request' "
        "AND json_extract(event_json,'$.request_encoding') IS NULL ORDER BY sequence"
    )]
    for sequence in sequences:
        org, raw = db.execute("SELECT organization_id,event_json FROM llm_events WHERE sequence=?", (sequence,)).fetchone()
        event = json.loads(raw)
        stored = stored_request(db, org, event)
        if stored is not event:
            db.execute("UPDATE llm_events SET event_json=? WHERE sequence=?", (json.dumps(stored, ensure_ascii=False), sequence))
    return len(sequences)
