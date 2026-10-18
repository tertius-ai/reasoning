"""Allowlisted, topic-scoped runtime counters for authorized developer bots.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .store import StoreError

IDENTITY = re.compile(r"[a-f0-9]{32}\Z")
STATES = (
    "queued", "running", "waiting_approval", "awaiting_approval", "succeeded", "failed",
    "cancelled", "interrupted", "outcome_unknown", "active", "paused", "complete",
)
ERRORS = (
    "cli_failed", "runner_lost", "access_changed", "agent_incomplete", "execution_failed",
    "resource_limit", "wall_time_limit", "log_limit", "artifact_limit", "image_verification_failed",
    "worker_stopped", "provider_timeout", "provider_unavailable", "provider_rate_limit",
    "provider_insufficient_credit", "provider_authentication", "provider_permission",
    "provider_request_rejected", "provider_model_unavailable", "provider_context_limit",
    "provider_stream_error", "provider_stream_incomplete", "context_limit", "backup_restored",
    "provider_rejected", "provider_protocol", "catalog_unavailable", "model_unavailable",
    "model_not_text", "model_output_limit_unsupported", "provider_key_invalid", "unsupported_tool_call",
)


def deployment_identity(frontend, release):
    """Expose only validated immutable identities, never manifest contents or file paths."""
    identity = release_identity(frontend, release)
    return {"release_id": identity["release_id"], "source_revision": identity["source_revision"]}


def release_identity(frontend, release):
    """The deployment identity plus `source_modified`, the verified manifest's uncommitted-changes flag.

    `source_revision` and `source_modified` are None unless the release's manifest SHA-256 equals `release`.
    """
    result = {"release_id": release, "source_revision": None, "source_modified": None}
    if not release or not re.fullmatch(r"[a-f0-9]{64}", release) or frontend is None:
        return {"release_id": None, "source_revision": None, "source_modified": None}
    try:
        manifest = Path(frontend).parent / "manifest.json"
        descriptor = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                return result
            body = stream.read(1024 * 1024 + 1)
            if len(body) != info.st_size or len(body) > 1024 * 1024:
                return result
        if hashlib.sha256(body).hexdigest() != release:
            return result
        document = json.loads(body)
        if not isinstance(document, dict):
            return result
        revision = document.get("source_revision")
        if isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
            result["source_revision"] = revision
            if isinstance(document.get("dirty"), bool):
                result["source_modified"] = document["dirty"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return result


def _buckets(db, query, parameters, values):
    slots = ",".join("?" for _ in values)
    rows = db.execute(
        f"SELECT CASE WHEN value IN ({slots}) THEN value ELSE 'other' END AS bucket, COUNT(*) "
        f"FROM ({query}) AS scoped_values GROUP BY 1", (*values, *parameters),
    )
    return {row[0]: row[1] for row in rows}


def collect(db, org, topic):
    """Caller must authorize its leased bot and requester before collection.

    This collector never reads content, credentials, logs, model names, addresses,
    command lines, or another topic. Free-text states/error codes are bucketed
    through explicit allowlists; even a syntactically valid secret becomes other.
    """
    if (not isinstance(org, str) or not IDENTITY.fullmatch(org)
            or not isinstance(topic, str) or not IDENTITY.fullmatch(topic)):
        raise StoreError(400, "invalid developer diagnostic scope")
    if db.execute("SELECT 1 FROM topics WHERE organization_id=? AND id=?", (org, topic)).fetchone() is None:
        raise StoreError(404, "developer diagnostic topic unavailable")
    version = db.schema_version() if getattr(db, "dialect", None) == "postgresql" else db.execute("PRAGMA user_version").fetchone()[0]
    count = db.execute("SELECT COUNT(*) FROM messages WHERE organization_id=? AND topic_id=?", (org, topic)).fetchone()[0]
    result = {"format": 1, "scope": "current_topic", "sanitized": True, "database_schema": version,
              "message_count": count,
              "runs": {}}
    queries = {
        "inference": "SELECT r.{field} AS value FROM llm_runs r JOIN llm_requests q ON q.id=r.request_id "
                     "AND q.organization_id=r.organization_id WHERE q.organization_id=? AND q.topic_id=?",
        "bots": "SELECT {field} AS value FROM bot_runs WHERE organization_id=? AND topic_id=?",
        "compute": "SELECT {field} AS value FROM compute_jobs WHERE organization_id=? AND topic_id=?",
        "workflows": "SELECT {field} AS value FROM workflow_runs WHERE organization_id=? AND topic_id=?",
        "agents": "SELECT {field} AS value FROM topic_agent_threads WHERE organization_id=? AND topic_id=?",
    }
    for kind, query in queries.items():
        counts = {"states": _buckets(db, query.format(field="status"), (org, topic), STATES)}
        if kind in {"inference", "bots", "compute"}:
            counts["errors"] = _buckets(db, query.format(field="error_code") + " AND " + (
                "r.error_code" if kind == "inference" else "error_code") + " IS NOT NULL", (org, topic), ERRORS)
        result["runs"][kind] = counts
    return result
