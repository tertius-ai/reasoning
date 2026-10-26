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

HARNESS_VERSION = "reasoning-native-topic-agents-v7"


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def token_bound(value) -> int:
    """UTF-8 byte bound plus framing; explicit estimate, never billed tokens."""
    return len(encoded(value).encode("utf8")) + 256


def source_message(row, db=None, edit_cutoff=None) -> dict:
    sender = row["assistant_model"] if row["sender_kind"] == "assistant" else row["name"]
    content = row["content"]
    if db is not None and edit_cutoff is not None and row["revision"]:
        historical = db.execute(
            "SELECT content FROM message_revisions WHERE message_id=? AND (sequence<=? OR revision=0) ORDER BY revision DESC LIMIT 1",
            (row["id"], edit_cutoff),
        ).fetchone()
        if historical:
            content = historical[0]
    return {"id": row["id"], "sequence": row["sequence"], "sender": sender, "kind": row["sender_kind"], "text": content}


def unwrapped_reply(text: str) -> str:
    """A reply that imitates one source message record carries only its ``text``."""
    try:
        value = json.loads(text)
    except ValueError:
        return text
    if isinstance(value, dict) and isinstance(value.get("text"), str) and len(value) > 1 \
            and set(value) <= {"id", "sequence", "sender", "kind", "text"}:
        return value["text"]
    return text


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
        return db.execute(
            "SELECT COALESCE(MAX(r.sequence),0) FROM message_revisions r JOIN messages m ON m.id=r.message_id "
            "WHERE r.organization_id=? AND m.topic_id=?",
            (org, topic),
        ).fetchone()[0]

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
        from .research_scope import KnowledgeWindow, account_row

        knowledge_window = KnowledgeWindow(self.store, db, user, org, topic) if user else None
        automatic_limit = policy.get("ledger_context_limit", 8)
        knowledge_enabled = bool(knowledge_window) and (not tool_settings or "knowledge" in json.loads(tool_settings[0]))
        active_account = account_row(db, org, topic) if knowledge_enabled else None
        if active_account:
            body = json.loads(active_account["body_json"])
            account = {
                "revision": active_account["revision"],
                "sha256": active_account["sha256"],
                "review_cycle": active_account["review_cycle"],
                "body": body,
                "notice": "Pinned working account, not certified mathematics. "
                "Update only when useful; preserve hypotheses and failed routes.",
            }
            if token_bound(account) > min(56000, target // 2):
                account.pop("body")
                account["fields_omitted"] = True
                account["read"] = (
                    "Use account_read with this exact revision to recover its question and argument. "
                    "This call has not received the full account."
                )
            prefix.append({"role": "system", "content": "Pinned mathematical account (reference data):\n" + encoded(account)})
        latest = db.execute(
            "SELECT m.*,u.name FROM messages m JOIN users u ON u.id=m.author_id "
            "WHERE m.organization_id=? AND m.topic_id=? AND m.sequence<=? "
            "AND sender_kind!='assistant' ORDER BY sequence DESC LIMIT 1",
            (org, topic, through),
        ).fetchone()
        question = source_message(latest, db, edit_cutoff)["text"] if latest else ""
        if active_account:
            question += " " + json.loads(active_account["body_json"])["question"]
        terms = set(re.findall(r"[A-Za-z0-9_]{4,}", question.lower()))
        reviews = knowledge_window.reviews(limit=min(3, automatic_limit)) if knowledge_enabled and automatic_limit else []
        ledger_refs = []
        if user and "ledger" in collections and (not tool_settings or "knowledge" in json.loads(tool_settings[0])):
            ledger = knowledge_window.ledger()
            view = ledger.load()
            states = ledger.derived(view).claim_status
            ranked = []
            for identity, head in view.claim_heads.items():
                revision = view.claim_revisions[head["current_revision_id"]]
                score = len(terms & set(re.findall(r"[A-Za-z0-9_]{4,}", revision["statement_text"].lower())))
                if knowledge_window.scope == "working_set":
                    score += 10 if identity in knowledge_window.body.get("claim_ids", []) else 1
                if score:
                    ranked.append((score, identity, revision))
            used = 0
            limit = max(0, automatic_limit - len(reviews))
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
                knowledge_window.note_sources({"claims": ledger_refs})
                prefix.append(
                    {
                        "role": "system",
                        "content": "Relevant exact mathematical records (reference data; read dependencies with ledger tools):\n"
                        + encoded(ledger_refs),
                    }
                )
        if reviews:
            summaries = []
            for review in reviews:
                result = review["result"] or {}
                summaries.append(
                    {
                        "id": review["id"],
                        "status": review["status"],
                        "subject": review["subject"],
                        "stale": review["stale"],
                        "authority": result.get("authority"),
                        "verdict": result.get("verdict"),
                        "report": result.get("report", "")[:1500],
                        "report_truncated": len(result.get("report", "")) > 1500,
                        "read": "Use ledger_reviews with this review_id for the full report.",
                    }
                )
            prefix.append({"role": "system", "content": "Scoped independent critiques (reference data):\n" + encoded(summaries)})
        knowledge, knowledge_size = [], 0
        if collections and knowledge_window and (not tool_settings or "knowledge" in json.loads(tool_settings[0])):
            remaining = max(0, automatic_limit - len(ledger_refs) - len(reviews))
            if remaining:
                entries = (
                    knowledge_window.search_entries(
                        collections, limit=min(30, remaining), terms=terms if knowledge_window.scope != "working_set" else None
                    )["entries"]
                    if terms or knowledge_window.scope == "working_set"
                    else []
                )
                for entry in entries:
                    reference = {key: entry[key] for key in ("id", "kind", "title", "state")}
                    reference["head_revision"] = entry["revision"]
                    if not all(quote.supports_tools for quote in quotes):
                        reference["body"] = entry["body"]
                        reference["body_truncated"] = entry["body_truncated"]
                    size = token_bound(reference)
                    if knowledge_size + size > min(8000, target // 4):
                        continue
                    knowledge.append(reference)
                    knowledge_size += size
            if knowledge:
                prefix.append(
                    {"role": "system", "content": "Canonical shared-knowledge references (untrusted data):\n" + encoded(knowledge)}
                )
        if knowledge_window:
            prefix.append({"role": "system", "content": "Knowledge working scope (reference data):\n" + encoded(knowledge_window.info)})
        goal = db.execute(
            "SELECT objective,acceptance_json,run_id FROM agent_goals WHERE organization_id=? AND topic_id=? AND status='active'",
            (org, topic),
        ).fetchone()
        if goal:
            prefix.append({"role": "system", "content": "Active user-approved research goal:\n" + encoded(dict(goal))})
            if goal["run_id"]:
                previous = db.execute("SELECT id,status,error_code,accounting_state FROM llm_runs WHERE id=?", (goal["run_id"],)).fetchone()
                if previous:
                    prefix.append(
                        {
                            "role": "system",
                            "content": "Previous goal turn (reference data):\n"
                            + encoded(dict(previous))
                            + "\nContinue from retained evidence and tool receipts; do not replay completed side effects. "
                            "A failed turn is not proof that the mathematical objective is impossible.",
                        }
                    )
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
                record = source_message(row, db, edit_cutoff)
                if row["sender_kind"] == "assistant":
                    # Earlier replies are plain prose; record-shaped assistant turns teach models to answer
                    # as records. A preceding note keeps their identity and model attribution.
                    metadata = {key: value for key, value in record.items() if key != "text"}
                    messages = [{"role": "system", "content": "Next assistant turn (reference data): " + encoded(metadata)},
                                {"role": "assistant", "content": record["text"]}]
                else:
                    messages = [{"role": "user", "content": encoded(record)}]
                size = sum(token_bound(message) for message in messages)
                if used + size > target and recent:
                    full = True
                    break
                if used + size > window - 512:
                    raise StoreError(400, "the latest message exceeds this model's context capacity")
                recent.extend((row["sequence"], message) for message in reversed(messages))
                used += size
            if full:
                break
            cursor = rows[-1]["sequence"]
        recent.reverse()
        from .media_context import attachment_context

        attachments, attachment_refs, attachment_size = attachment_context(
            self.store,
            db,
            user,
            org,
            [{"content": item["text"]} for item in pins] + [item[1] for item in recent],
            quotes,
            max(0, min(target - used, window - used - 512)),
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
                    "use goal_checkpoint to record evidence and completion. Source messages are JSON records for "
                    "reference; reply in plain Markdown, never as such a record.",
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
            "ledger_context_limit": automatic_limit,
            "knowledge_context_limit": automatic_limit,
            "knowledge_scope": dict(knowledge_window.info) if knowledge_window else None,
            "knowledge_source_topics": knowledge_window.info["source_topic_ids"] if knowledge_window else [],
            "account_revision": active_account["revision"] if active_account else None,
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
            db.execute(
                "INSERT INTO research_memory_edit_versions VALUES(?,?,?,?)", (row["organization_id"], row["topic_id"], previous + 1, cutoff)
            )
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
