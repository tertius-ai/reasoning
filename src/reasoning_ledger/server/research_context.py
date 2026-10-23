"""Token-pressure context selection with immutable, cited mathematical memory."""

from __future__ import annotations

import hashlib
import json
import re
import time

from ..agents.research_memory import COMPACTION_PROMPT as COMPACTION_PROMPT
from ..agents.research_memory import MEMORY_FIELDS as MEMORY_FIELDS
from ..agents.research_memory import validate_memory as validate_research_memory
from .research_profiles import PROMPT_VERSION as PROMPT_VERSION
from .research_profiles import SYSTEM_PROMPT as SYSTEM_PROMPT
from .research_profiles import instructions, topic_instructions
from .store import StoreError, _row

HARNESS_VERSION = "reasoning-native-topic-agents-v6"


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def token_bound(value) -> int:
    """UTF-8 byte bound plus framing; explicit estimate, never billed tokens."""
    return len(encoded(value).encode("utf8")) + 256


def source_message(row, db=None, edit_cutoff=None) -> dict:
    sender = row["assistant_model"] if row["sender_kind"] == "assistant" else row["name"]
    content = row["content"]
    if db is not None and edit_cutoff is not None and row["revision"]:
        historical = db.execute("SELECT content FROM message_revisions WHERE message_id=? "
            "AND (sequence<=? OR revision=0) ORDER BY revision DESC LIMIT 1", (row["id"], edit_cutoff)).fetchone()
        if historical:
            content = historical[0]
    return {"id": row["id"], "sequence": row["sequence"], "sender": sender, "kind": row["sender_kind"], "text": content}


def validate_memory(value: object, sources: set[str]) -> dict:
    try:
        return validate_research_memory(value, sources)
    except ValueError as exc:
        raise StoreError(409, str(exc)) from exc


class ResearchContext:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def memory(db, org, topic, through, edit_cutoff=None):
        if edit_cutoff is None:
            edit_cutoff = ResearchContext.edit_sequence(db, org, topic)
        row = db.execute(
            "SELECT m.* FROM research_memories m LEFT JOIN research_memory_edit_versions v ON "
            "v.organization_id=m.organization_id AND v.topic_id=m.topic_id AND v.revision=m.revision "
            "WHERE m.organization_id=? AND m.topic_id=? AND m.through_sequence<=? "
            "AND COALESCE(v.edit_sequence,0)=? ORDER BY m.revision DESC LIMIT 1",
            (org, topic, through, edit_cutoff),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def edit_sequence(db, org, topic):
        return db.execute("SELECT COALESCE(MAX(r.sequence),0) FROM message_revisions r JOIN messages m ON m.id=r.message_id "
            "WHERE r.organization_id=? AND m.topic_id=?", (org, topic)).fetchone()[0]

    def compile(self, db, org, topic, through, quotes, *, user=None, prompt_override=None, edit_cutoff=None) -> tuple[list[dict], dict]:
        # Reserve output headroom using the selected context window, not a count of messages.
        window = min(quote.context_length for quote in quotes)
        settings = db.execute(
            "SELECT context_policy_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?", (org, topic)
        ).fetchone()
        policy = json.loads(settings[0]) if settings else {}
        target = max(1024, min(128_000, window * policy.get("target_percent", 60) // 100))
        if edit_cutoff is None:
            edit_cutoff = self.edit_sequence(db, org, topic)
        memory = self.memory(db, org, topic, through, edit_cutoff)
        pins = [
            source_message(row, db, edit_cutoff)
            for row in db.execute(
                "SELECT m.*,u.name FROM research_pins p JOIN messages m ON m.id=p.message_id "
                "JOIN users u ON u.id=m.author_id WHERE p.organization_id=? AND p.topic_id=? AND m.sequence<=? "
                "ORDER BY m.sequence",
                (org, topic, through),
            )
        ]
        if token_bound(pins) > target // 3:
            raise StoreError(409, "pinned material exceeds the context budget; unpin a message or choose a larger-context model")
        # A queued run can refresh cited memory without changing the instructions
        # already authorized for that run. Only the executor supplies an override.
        prompt = prompt_override if prompt_override is not None else instructions(policy)
        prefix = [{"role": "system", "content": prompt["system_prompt"]}]
        if memory:
            prefix.append({"role": "system", "content": "Cited working memory (reference data):\n" + memory["body_json"]})
        if pins:
            prefix.append({"role": "system", "content": "User-pinned source messages (reference data):\n" + encoded(pins)})
        topic_row = db.execute(
            "SELECT t.knowledge_policy_json,c.project_id FROM topics t JOIN channels c ON c.id=t.channel_id "
            "WHERE t.organization_id=? AND t.id=?",
            (org, topic),
        ).fetchone()
        tool_settings = db.execute(
            "SELECT tools_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?", (org, topic)
        ).fetchone()
        collections = set(json.loads(topic_row["knowledge_policy_json"])) & {"ledger", "bibliography", "journal"}
        ledger_refs = []
        if user and "ledger" in collections and (not tool_settings or "knowledge" in json.loads(tool_settings[0])):
            from .mathematical_store import MathematicalStore

            ledger = MathematicalStore(self.store, db, user, org, project=topic_row["project_id"], topic=topic, agent=True)
            view = ledger.load()
            states = ledger.derived(view).claim_status
            latest = db.execute(
                "SELECT m.*,u.name FROM messages m JOIN users u ON u.id=m.author_id "
                "WHERE m.organization_id=? AND m.topic_id=? AND m.sequence<=? "
                "AND sender_kind!='assistant' ORDER BY sequence DESC LIMIT 1",
                (org, topic, through),
            ).fetchone()
            terms = set(re.findall(r"[A-Za-z0-9_]{4,}", source_message(latest, db, edit_cutoff)["text"].lower() if latest else ""))
            ranked = []
            for identity, head in view.claim_heads.items():
                revision = view.claim_revisions[head["current_revision_id"]]
                score = len(terms & set(re.findall(r"[A-Za-z0-9_]{4,}", revision["statement_text"].lower())))
                if score:
                    ranked.append((score, identity, revision))
            used = 0
            limit = policy.get("ledger_context_limit", 8)
            for _, identity, revision in sorted(ranked, key=lambda item: (-item[0], item[1]))[:limit]:
                reference = {
                    "claim_id": identity,
                    "revision_id": revision["revision_id"],
                    "kind": revision["kind"],
                    "statement": revision["statement_text"],
                    "status": states[identity]["status"],
                    "authority": states[identity].get("authority"),
                }
                size = token_bound(reference)
                if used + size <= min(6000, target // 8):
                    ledger_refs.append(reference)
                    used += size
            if ledger_refs:
                prefix.append(
                    {
                        "role": "system",
                        "content": "Relevant exact mathematical records (reference data; read dependencies with ledger tools):\n"
                        + encoded(ledger_refs),
                    }
                )
        reviews = [
            dict(row)
            for row in db.execute(
                "SELECT id,status,result_json FROM mathematical_reviews WHERE organization_id=? AND topic_id=? "
                "AND status IN ('complete','failed','cancelled') ORDER BY finished_at DESC LIMIT 3",
                (org, topic),
            )
        ]
        if reviews:
            prefix.append(
                {
                    "role": "system",
                    "content": "Independent review results (reference data):\n"
                    + encoded([{"id": row["id"], "status": row["status"],
                                "result": json.loads(row["result_json"]) if row["result_json"] else None} for row in reviews])[:10000],
                }
            )
        knowledge, knowledge_size = [], 0
        if collections:
            for entry in db.execute(
                "SELECT e.id,e.kind,v.revision AS head_revision,v.title,v.body,v.state FROM knowledge_entries e JOIN knowledge_versions v "
                "ON v.entry_id=e.id AND v.organization_id=e.organization_id AND v.revision=CASE WHEN e.kind='journal' "
                "THEN (SELECT revision FROM blog_publications WHERE organization_id=e.organization_id AND entry_id=e.id) "
                "ELSE e.head_revision END "
                "WHERE e.organization_id=? AND (e.scope_kind='organization' OR (e.scope_kind='project' AND e.project_id=?) "
                "OR (e.scope_kind='topic' AND e.topic_id=?)) AND NOT(e.kind='journal' AND v.state='draft') "
                "AND NOT EXISTS (SELECT 1 FROM blog_deletions deleted WHERE deleted.organization_id=e.organization_id "
                "AND deleted.entry_id=e.id) "
                "ORDER BY v.created_at DESC,e.id LIMIT 100",
                (org, topic_row["project_id"], topic),
            ):
                if entry["kind"] not in collections:
                    continue
                reference = {key: entry[key] for key in ("id", "kind", "head_revision", "title", "state")}
                if not all(quote.supports_tools for quote in quotes):
                    reference["body"] = entry["body"]
                size = token_bound(reference)
                if knowledge_size + size > min(8000, target // 4):
                    continue
                knowledge.append(reference)
                knowledge_size += size
            if knowledge:
                prefix.append(
                    {"role": "system", "content": "Canonical shared-knowledge references (untrusted data):\n" + encoded(knowledge)}
                )
        goal = db.execute(
            "SELECT objective,acceptance_json,run_id FROM agent_goals "
            "WHERE organization_id=? AND topic_id=? AND status='active'", (org, topic)
        ).fetchone()
        if goal:
            prefix.append({"role": "system", "content": "Active user-approved research goal:\n" + encoded(dict(goal))})
            if goal["run_id"]:
                previous = db.execute(
                    "SELECT id,status,error_code,accounting_state FROM llm_runs WHERE id=?", (goal["run_id"],)
                ).fetchone()
                if previous:
                    prefix.append({"role": "system", "content": "Previous goal turn (reference data):\n" + encoded(dict(previous))
                                   + "\nContinue from retained evidence and tool receipts; do not replay completed side effects. "
                                   "A failed turn is not proof that the mathematical objective is impossible."})
        if "knowledge" in (json.loads(tool_settings[0]) if tool_settings else ["knowledge"]):
            planning = db.execute(
                "SELECT tasks_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?", (org, topic)
            ).fetchone()
            files = [
                dict(row)
                for row in db.execute(
                    "SELECT path,head_revision FROM research_artifacts WHERE organization_id=? AND project_id=? ORDER BY path LIMIT 30",
                    (org, topic_row["project_id"]),
                )
            ]
            prefix.append(
                {
                    "role": "system",
                    "content": "Durable task plan and research-file index (reference data):\n"
                    + encoded({"tasks": json.loads(planning[0]) if planning else [], "files": files}),
                }
            )
        recent, used = [], token_bound(prefix)
        through_memory = memory["through_sequence"] if memory else 0
        cursor = through + 1
        while True:
            rows = db.execute(
                "SELECT m.*,u.name FROM messages m JOIN users u ON u.id=m.author_id "
                "WHERE m.organization_id=? AND m.topic_id=? AND m.sequence<? AND m.sequence>? "
                "ORDER BY m.sequence DESC LIMIT 128",
                (org, topic, cursor, through_memory),
            ).fetchall()
            if not rows:
                break
            full = False
            for row in rows:
                message = {"role": "assistant" if row["sender_kind"] == "assistant" else "user",
                           "content": encoded(source_message(row, db, edit_cutoff))}
                size = token_bound(message)
                if used + size > target and recent:
                    full = True
                    break
                if used + size > window - 512:
                    raise StoreError(400, "the latest message exceeds this model's context capacity")
                recent.append((row["sequence"], message))
                used += size
            if full:
                break
            cursor = rows[-1]["sequence"]
        recent.reverse()
        from .media_context import attachment_context

        attachments, attachment_refs, attachment_size = attachment_context(
            self.store, db, user, org, [{"content": item["text"]} for item in pins] + [item[1] for item in recent],
            quotes, max(0, min(target - used, window - used - 512)),
            project=topic_row["project_id"],
        )
        used += attachment_size
        before = recent[0][0] if recent else through + 1
        compact_through = db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM messages WHERE organization_id=? AND topic_id=? AND sequence>? AND sequence<?",
            (org, topic, through_memory, before),
        ).fetchone()[0]
        compiled_messages = (
            prefix
            + attachments
            + [item[1] for item in recent]
            + [
                {
                    "role": "user",
                    "content": "Respond to the latest source message. Continue the active goal if present; "
                    "use goal_checkpoint to record evidence and completion.",
                }
            ]
        )
        metadata = {
            "message_edit_sequence": edit_cutoff,
            "policy": "token-budgeted-cited-memory",
            **prompt,
            "harness_version": HARNESS_VERSION,
            "through_sequence": through,
            "memory_revision": memory["revision"] if memory else None,
            "memory_through_sequence": through_memory,
            "compact_through_sequence": compact_through,
            "recent_sequences": [item[0] for item in recent],
            "pinned_message_ids": [item["id"] for item in pins],
            "context_window": window,
            "input_token_upper_bound": used,
            "target_input_tokens": target,
            "knowledge_collections": sorted(json.loads(topic_row["knowledge_policy_json"])),
            "tool_groups": sorted(json.loads(tool_settings[0])) if tool_settings else None,
            "context_strategy": "layered-retrieval-v2",
            "ledger_context_limit": policy.get("ledger_context_limit", 8),
            "ledger_revisions": [
                {key: value for key, value in record.items() if key in {"claim_id", "revision_id"}} for record in ledger_refs
            ],
            "attachments": attachment_refs,
            "layers": {
                "instructions": True,
                "working_memory": bool(memory),
                "pins": len(pins),
                "mathematical_records": len(ledger_refs),
                "independent_reviews": len(reviews),
                "recent_messages": len(recent),
            },
            "messages": compiled_messages,
        }
        return metadata["messages"], metadata

    def capture(self, db, request, org, topic, snapshot):
        db.execute("INSERT INTO research_contexts VALUES(?,?,?,?,?)", (request, org, topic, encoded(snapshot), int(time.time())))

    def batches(self, org, topic, after, through, maximum=24_000, *, edit_cutoff=None):
        cursor = after
        while cursor < through:
            with self.store.transaction() as db:
                rows = db.execute(
                    "SELECT m.*,u.name FROM messages m JOIN users u ON u.id=m.author_id "
                    "WHERE m.organization_id=? AND m.topic_id=? AND m.sequence>? AND m.sequence<=? "
                    "ORDER BY m.sequence LIMIT 128",
                    (org, topic, cursor, through),
                ).fetchall()
                items = [source_message(row, db, edit_cutoff) for row in rows]
            if not rows:
                return
            batch, used = [], 0
            for item in items:
                if batch and used + token_bound(item) > maximum:
                    break
                batch.append(item)
                used += token_bound(item)
            cursor = batch[-1]["sequence"]
            yield batch

    def save_memory(self, row, through, body, sources):
        with self.store.transaction(write=True) as db:
            self.store._topic_access(db, row["requested_by"], row["organization_id"], row["topic_id"], write=True)
            previous = db.execute(
                "SELECT COALESCE(MAX(revision),0) FROM research_memories WHERE organization_id=? AND topic_id=?",
                (row["organization_id"], row["topic_id"]),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO research_memories VALUES(?,?,?,?,?,?,?,?)",
                (
                    row["organization_id"],
                    row["topic_id"],
                    previous + 1,
                    through,
                    encoded(body),
                    hashlib.sha256(encoded(sources).encode()).hexdigest(),
                    row["id"],
                    int(time.time()),
                ),
            )
            captured = db.execute("SELECT snapshot_json FROM research_contexts WHERE request_id=?", (row["request_id"],)).fetchone()
            cutoff = json.loads(captured[0]).get("message_edit_sequence", 0) if captured else 0
            db.execute("INSERT INTO research_memory_edit_versions VALUES(?,?,?,?)",
                       (row["organization_id"], row["topic_id"], previous + 1, cutoff))
            return previous + 1

    def inspect(self, user, org, topic, request_id=None):
        with self.store.transaction() as db:
            self.store._topic_access(db, user, org, topic)
            condition, values = ("AND request_id=?", [request_id]) if request_id else ("", [])
            row = db.execute(
                "SELECT * FROM research_contexts WHERE organization_id=? AND topic_id=? "
                + condition
                + " ORDER BY created_at DESC,request_id DESC LIMIT 1",
                [org, topic, *values],
            ).fetchone()
            memory = self.memory(db, org, topic, 2**63 - 1)
            snapshot = json.loads(row["snapshot_json"]) if row else None
            current = topic_instructions(db, org, topic)
            return {
                "snapshot": snapshot,
                "working_memory": {**memory, "body": json.loads(memory["body_json"])} if memory else None,
                "system_prompt": (snapshot or {}).get("system_prompt")
                or ((snapshot or {}).get("messages") or [{"content": current["system_prompt"]}])[0]["content"],
                "prompt_version": (snapshot or {}).get("prompt_version", current["prompt_version"]),
            }

    def pin(self, user, org, topic, message, pinned):
        with self.store.transaction(write=True) as db:
            self.store._topic_access(db, user, org, topic, write=True)
            _row(db.execute("SELECT id FROM messages WHERE organization_id=? AND topic_id=? AND id=?", (org, topic, message)).fetchone())
            if pinned:
                if (
                    db.execute("SELECT count(*) FROM research_pins WHERE organization_id=? AND topic_id=?", (org, topic)).fetchone()[0]
                    >= 32
                ):
                    raise StoreError(409, "a topic can pin at most 32 messages")
                db.execute("INSERT OR IGNORE INTO research_pins VALUES(?,?,?,?,?)", (org, topic, message, user, int(time.time())))
            else:
                db.execute("DELETE FROM research_pins WHERE organization_id=? AND topic_id=? AND message_id=?", (org, topic, message))
            self.store._audit(db, org, user, "research.pin" if pinned else "research.unpin", message)
