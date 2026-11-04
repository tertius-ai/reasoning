"""Explicitly save retained file text without replaying a failed provider call."""

import hashlib
import json
import re
import time

from .research_artifacts import ResearchArtifacts, safe_path
from .research_file_limits import MAX_ARTIFACT_TEXT_BYTES, MAX_TOOL_ARGUMENT_BYTES
from .store import StoreError, _row


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous retained arguments")
        result[key] = value
    return result


def _file_arguments(call):
    """Only extract complete, bounded artifact_write JSON. Never invoke a tool."""
    try:
        function = call["function"]
        raw = function["arguments"]
        if not isinstance(raw, str) or len(raw.encode("utf8")) > MAX_TOOL_ARGUMENT_BYTES:
            return None
        arguments = json.loads(raw, object_pairs_hook=_unique_object)
        if function["name"] == "tool_call":
            if set(arguments) != {"name", "arguments"} or arguments["name"] != "artifact_write":
                return None
            arguments = arguments["arguments"]
        elif function["name"] != "artifact_write":
            return None
        if not isinstance(arguments, dict) or not {"path", "text"} <= arguments.keys():
            return None
        if arguments.keys() - {"path", "text", "expected_sha256"}:
            return None
        text, expected = arguments["text"], arguments.get("expected_sha256")
        if not safe_path(arguments["path"]) or not isinstance(text, str) or "\0" in text:
            return None
        if len(text.encode("utf8")) > MAX_ARTIFACT_TEXT_BYTES:
            return None
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected)):
            return None
        return arguments
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        return None


def _copy_path(path, sequence, index):
    basename = path.rsplit("/", 1)[-1]
    stem, dot, suffix = basename.rpartition(".")
    if not dot or len(suffix) > 20:
        stem, suffix = basename, ""
    tag = f".recovered-{sequence}-{index}" + ("." + suffix if suffix else "")
    # A safe basename avoids exceeding the relative-path limit for deep original paths.
    return stem.rsplit("/", 1)[-1][:200 - len(tag)] + tag


def _calls(row):
    if row["calls_type"] != "array":
        return []
    try:
        calls = json.loads(row["calls_json"] or "null")
        return calls if isinstance(calls, list) else []
    except (TypeError, ValueError, RecursionError):
        return []


class ResearchArtifactRecovery:
    def __init__(self, store):
        self.store = store
        self.artifacts = ResearchArtifacts(store)

    @staticmethod
    def _events(db, org, topic, sequence=None):
        return db.execute(
            "SELECT e.sequence,e.run_id,json_extract(e.event_json,'$.response.choices[0].message.tool_calls') AS calls_json,"
            "json_type(e.event_json,'$.response.choices[0].message.tool_calls') AS calls_type "
            "FROM llm_events e JOIN llm_runs r ON r.organization_id=e.organization_id AND r.id=e.run_id "
            "JOIN llm_requests q ON q.organization_id=r.organization_id AND q.id=r.request_id "
            "WHERE e.organization_id=? AND q.topic_id=? AND r.status IN ('failed','cancelled','interrupted') "
            "AND json_extract(e.event_json,'$.phase')='provider_partial_response' "
            + ("AND e.sequence=? " if sequence is not None else "")
            + "ORDER BY e.sequence DESC LIMIT 50",
            (org, topic, sequence) if sequence is not None else (org, topic),
        )

    def list(self, user, org, topic):
        with self.store.transaction() as db:
            self.artifacts._topic(db, user, org, topic)
            candidates = []
            for row in self._events(db, org, topic):
                for index, call in enumerate(_calls(row)[:10001]):
                    arguments = _file_arguments(call)
                    if arguments is None:
                        continue
                    retained = db.execute(
                        "SELECT result_json FROM research_artifact_recoveries "
                        "WHERE organization_id=? AND event_sequence=? AND tool_index=?",
                        (org, row["sequence"], index),
                    ).fetchone()
                    if retained:
                        continue
                    body = arguments["text"].encode("utf8")
                    candidates.append({
                        "event_sequence": row["sequence"], "tool_index": index, "run_id": row["run_id"],
                        "path": arguments["path"], "recovery_path": _copy_path(arguments["path"], row["sequence"], index),
                        "total_characters": len(arguments["text"]), "size_bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                    })
                    if len(candidates) == 50:
                        return candidates
            return candidates

    def recover(self, user, org, topic, event_sequence, tool_index, sha256, path=None):
        if type(event_sequence) is not int or event_sequence < 1 or type(tool_index) is not int or tool_index < 0:
            raise StoreError(400, "select an exact retained file")
        with self.store.transaction(write=True) as db:
            project = self.artifacts._topic(db, user, org, topic, write=True)
            if self.store._postgres:
                self.store._postgres.lock(db.native, f"artifact-recovery:{org}:{event_sequence}:{tool_index}")
            row = _row(self._events(db, org, topic, event_sequence).fetchone())
            calls = _calls(row)
            arguments = _file_arguments(calls[tool_index]) if tool_index < len(calls) else None
            if arguments is None:
                raise StoreError(409, "this retained response does not contain a complete recoverable file")
            checksum = hashlib.sha256(arguments["text"].encode("utf8")).hexdigest()
            if checksum != sha256:
                raise StoreError(409, "retained file changed; refresh its recovery details")
            target = path if path is not None else _copy_path(arguments["path"], event_sequence, tool_index)
            old = db.execute(
                "SELECT result_json FROM research_artifact_recoveries WHERE organization_id=? AND event_sequence=? AND tool_index=?",
                (org, event_sequence, tool_index),
            ).fetchone()
            if old:
                saved = json.loads(old[0])
                if saved["path"] != target:
                    raise StoreError(409, "this retained file has already been recovered under a different name")
                return saved
            # Recovery makes a new copy; a diagnostic can never overwrite a user's repaired file.
            saved = self.artifacts._write(db, user, org, project, target, arguments["text"],
                                          topic=topic, run_id=row["run_id"], strict=True)
            from .research_artifacts import file_links

            result = {**saved, **file_links(org, topic, target, saved["revision"])}
            db.execute("INSERT INTO research_artifact_recoveries VALUES(?,?,?,?,?,?,?,?)",
                       (org, event_sequence, tool_index, row["run_id"], checksum, user, json.dumps(result), int(time.time())))
            self.store._audit(db, org, user, "research.artifact.recover", saved["id"])
            return result
