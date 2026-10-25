"""Permission-checked human reactions; model identity comes from retained outputs."""

import re
import time

from .reaction_emoji_schema import EMOJIS, MODEL_REPLY_EMOJIS
from .store import StoreError, _row

MESSAGE_BATCH = 200


def bot_source(db, row):
    """A bot-controlled client string alone is not inference provenance."""
    if row["sender_kind"] != "bot":
        return None
    matched = re.fullmatch(r"bot-run-([a-f0-9]{32})-([0-9]{1,8})", row["client_id"])
    if not matched:
        return None
    run = db.execute(
        "SELECT id,model,output_text FROM bot_runs WHERE organization_id=? AND topic_id=? AND id=? AND bot_id=?",
        (row["organization_id"], row["topic_id"], matched[1], row["author_id"]),
    ).fetchone()
    start = int(matched[2]) * 16000
    return run if run and run["output_text"][start : start + 16000] == row["content"] else None


class ChatFeedback:
    def __init__(self, store):
        self.store = store

    def target(self, db, user, org, topic, kind, target, node=None, *, write=False):
        self.store._topic_access(db, user, org, topic, write=write)
        return self._resolve(db, org, topic, kind, target, node)

    def _resolve(self, db, org, topic, kind, target, node=None):
        """Canonical feedback target within a topic whose access the caller has already checked."""
        if kind == "message":
            row = _row(
                db.execute("SELECT * FROM messages WHERE organization_id=? AND topic_id=? AND id=?", (org, topic, target)).fetchone()
            )
            # One physical model response has one feedback target, including its chat rendering.
            if row.get("inference_run_id"):
                return self._resolve(db, org, topic, "run", row["inference_run_id"])
            model = row["assistant_model"] if row["sender_kind"] == "assistant" else None
            provenance = db.execute(
                "SELECT source_model FROM fork_message_sources WHERE organization_id=? AND message_id=?", (org, target)
            ).fetchone()
            if provenance:
                model = provenance[0]
            bot = bot_source(db, row)
            if bot:
                return self._resolve(db, org, topic, "bot", bot["id"])
            return "message:" + target, model
        if kind == "run":
            row = _row(
                db.execute(
                    "SELECT r.id,r.model,r.output_text FROM llm_runs r JOIN llm_requests q ON q.id=r.request_id "
                    "WHERE r.organization_id=? AND q.topic_id=? AND r.id=?",
                    (org, topic, target),
                ).fetchone()
            )
            if not row["output_text"]:
                raise StoreError(409, "this response has no retained output to react to")
            return "run:" + target, row["model"]
        if kind == "bot":
            row = _row(
                db.execute(
                    "SELECT model,output_text FROM bot_runs WHERE organization_id=? AND topic_id=? AND id=?", (org, topic, target)
                ).fetchone()
            )
            if not row["output_text"]:
                raise StoreError(409, "this response has no retained output to react to")
            return "bot:" + target, row["model"]
        if kind == "node" and node:
            _row(
                db.execute("SELECT id FROM workflow_runs WHERE organization_id=? AND topic_id=? AND id=?", (org, topic, target)).fetchone()
            )
            row = _row(
                db.execute(
                    "SELECT * FROM workflow_node_attempts WHERE organization_id=? AND run_id=? AND node_id=? ORDER BY attempt DESC LIMIT 1",
                    (org, target, node),
                ).fetchone()
            )
            if row.get("inference_run_id"):
                return self._resolve(db, org, topic, "run", row["inference_run_id"])
            if row.get("bot_run_id"):
                return self._resolve(db, org, topic, "bot", row["bot_run_id"])
            if row["status"] not in ("succeeded", "failed"):
                raise StoreError(409, "this node has no retained result to react to")
            return f"node:{target}:{node}:{row['attempt']}", None
        raise StoreError(400, "invalid feedback target")

    def read(self, user, org, topic, kind, target, node=None):
        with self.store.transaction() as db:
            key, _ = self.target(db, user, org, topic, kind, target, node)
            return self._read(db, user, org, key)

    def read_messages(self, user, org, topic, message_ids):
        """Reactions for many messages of one topic after one access check.

        Messages that are absent from the topic or whose response has no retained output have no entry,
        matching the 404/409 the single-target read returns for them.
        """
        ids = list(dict.fromkeys(message_ids))
        if len(ids) > MESSAGE_BATCH:
            raise StoreError(400, f"request reactions for at most {MESSAGE_BATCH} messages")
        with self.store.transaction() as db:
            self.store._topic_access(db, user, org, topic)
            result = {}
            for message in ids:
                try:
                    key, _ = self._resolve(db, org, topic, "message", message)
                except StoreError as exc:
                    if exc.status not in (404, 409):
                        raise
                    continue
                result[message] = self._read(db, user, org, key)
            return result

    @staticmethod
    def _read(db, user, org, key):
        counts = {
            row["emoji"]: row["count"]
            for row in db.execute(
                "SELECT emoji,count(*) AS count FROM chat_reactions WHERE organization_id=? AND target_key=? GROUP BY emoji", (org, key)
            )
        }
        return {
            "target_key": key,
            "counts": {emoji: counts.get(emoji, 0) for emoji in EMOJIS},
            "reactors": {
                emoji: [dict(row) for row in db.execute(
                    "SELECT u.id,u.name FROM chat_reactions r JOIN users u ON u.id=r.user_id "
                    "WHERE r.organization_id=? AND r.target_key=? AND r.emoji=? ORDER BY r.created_at,r.user_id LIMIT 3",
                    (org, key, emoji),
                )] if counts.get(emoji) else []
                for emoji in EMOJIS
            },
            "mine": [
                row[0]
                for row in db.execute(
                    "SELECT emoji FROM chat_reactions WHERE organization_id=? AND target_key=? AND user_id=? ORDER BY emoji",
                    (org, key, user),
                )
            ],
        }

    def set(self, user, org, topic, kind, target, emoji, active, node=None):
        if emoji not in EMOJIS or not isinstance(active, bool):
            raise StoreError(400, "invalid human reaction")
        with self.store.transaction(write=True) as db:
            key, model = self.target(db, user, org, topic, kind, target, node, write=True)
            if _row(db.execute("SELECT account_kind FROM users WHERE id=?", (user,)).fetchone())["account_kind"] != "human":
                raise StoreError(403, "human feedback requires a human account")
            # Only a person's own message resolves to a message key without a model; everything else is model output.
            # Earlier reactions outside the three remain readable and removable.
            if active and emoji not in MODEL_REPLY_EMOJIS and (model or not key.startswith("message:")):
                raise StoreError(400, "react to a model reply with thumbs up, thumbs down or the upside-down smile")
            if active:
                db.execute(
                    "INSERT INTO chat_reactions VALUES(?,?,?,?,?,?,?) ON CONFLICT(organization_id,target_key,user_id) "
                    "DO UPDATE SET emoji=excluded.emoji,model=excluded.model,created_at=excluded.created_at "
                    "WHERE chat_reactions.emoji<>excluded.emoji",
                    (org, topic, key, user, emoji, model, int(time.time())),
                )
            else:
                db.execute(
                    "DELETE FROM chat_reactions WHERE organization_id=? AND target_key=? AND user_id=? AND emoji=?", (org, key, user, emoji)
                )
            return self._read(db, user, org, key)

    def totals(self, user, org, project=None):
        with self.store.transaction() as db:
            self.store._require(db, user, org)
            if project:
                self.store._project_access(db, user, org, project)
            rows = db.execute(
                "SELECT r.topic_id,r.model,r.emoji,count(*) AS count FROM chat_reactions r "
                "JOIN topics t ON t.id=r.topic_id JOIN channels c ON c.id=t.channel_id "
                "WHERE r.organization_id=? AND r.model IS NOT NULL"
                + (" AND c.project_id=?" if project else "")
                + " GROUP BY r.topic_id,r.model,r.emoji",
                (org, project) if project else (org,),
            )
            result = {}
            for row in rows:
                try:
                    self.store._topic_access(db, user, org, row["topic_id"])
                except StoreError:
                    continue
                item = result.setdefault(row["model"], {"model": row["model"], **dict.fromkeys(EMOJIS, 0)})
                item[row["emoji"]] += row["count"]
            return {"models": [result[key] for key in sorted(result)], "unit": "human reactions"}
