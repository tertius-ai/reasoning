"""Branch beta history without inheriting future memory, jobs, or spending."""

import time

from .chat_feedback import bot_source
from .store import StoreError, _id, _row


class TopicForks:
    def __init__(self, store):
        self.store = store

    def origin(self, user, org, topic):
        with self.store.transaction() as db:
            self.store._topic_access(db, user, org, topic)
            row = db.execute(
                "SELECT source_topic_id,source_message_id,through_sequence,created_by,created_at "
                "FROM topic_forks WHERE organization_id=? AND topic_id=?",
                (org, topic),
            ).fetchone()
            return dict(row) if row else None

    def create(self, user, org, topic_id, message_id, name, client_id):
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 200
            or not isinstance(client_id, str)
            or not 1 <= len(client_id) <= 128
        ):
            raise StoreError(400, "a fork requires a name and retry identity")
        name = name.strip()
        with self.store.transaction(write=True) as db:
            topic = self.store._topic_access(db, user, org, topic_id, write=True)[-1]
            if topic["kind"] != "beta":
                raise StoreError(400, "only beta conversations can be forked")
            source = _row(
                db.execute(
                    "SELECT id,sequence FROM messages WHERE organization_id=? AND topic_id=? AND id=?", (org, topic_id, message_id)
                ).fetchone()
            )
            existing = db.execute(
                "SELECT * FROM topic_forks WHERE organization_id=? AND created_by=? AND client_id=?", (org, user, client_id)
            ).fetchone()
            if existing:
                if existing["source_topic_id"] != topic_id or existing["source_message_id"] != message_id or existing["name"] != name:
                    raise StoreError(409, "fork retry identity was used with different inputs")
                self.store._topic_access(db, user, org, existing["topic_id"], write=True)
                return self._view(db, org, existing["topic_id"])
            new_id, now = _id(), int(time.time())
            db.execute(
                "INSERT INTO topics(id,organization_id,channel_id,name,kind,created_at,default_model,knowledge_policy_json,default_effort) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id, org, topic["channel_id"], name, "beta", now, topic["default_model"], topic["knowledge_policy_json"],
                 topic["default_effort"]),
            )
            # Copy settings, not tasks (which may refer to messages beyond the cutoff).
            db.execute(
                "INSERT INTO research_topic_settings(organization_id,topic_id,tools_json,reviewer_model,context_policy_json,"
                "compute_allowance_cpu_seconds) SELECT organization_id,?,tools_json,reviewer_model,context_policy_json,"
                "compute_allowance_cpu_seconds FROM research_topic_settings WHERE organization_id=? AND topic_id=?",
                (new_id, org, topic_id),
            )
            rows = list(
                db.execute(
                    "SELECT * FROM messages WHERE organization_id=? AND topic_id=? AND sequence<=? ORDER BY sequence",
                    (org, topic_id, source["sequence"]),
                )
            )
            copies = {}
            for row in rows:
                copied = copies[row["id"]] = _id()
                model = row["assistant_model"] if row["sender_kind"] == "assistant" else None
                bot = bot_source(db, row)
                if bot:
                    model = bot["model"]
                prior = db.execute(
                    "SELECT source_model FROM fork_message_sources WHERE organization_id=? AND message_id=?", (org, row["id"])
                ).fetchone()
                if prior:
                    model = prior[0]
                # Direct retained inserts deliberately emit neither bot events nor new inference requests.
                db.execute(
                    "INSERT INTO messages(id,organization_id,topic_id,author_id,content,client_id,created_at,sender_kind,"
                    "assistant_model,author_kind,reply_to) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        copied,
                        org,
                        new_id,
                        row["author_id"],
                        row["content"],
                        "fork-" + copied,
                        row["created_at"],
                        row["sender_kind"],
                        row["assistant_model"],
                        row["author_kind"],
                        copies.get(row["reply_to"]),
                    ),
                )
                db.execute("INSERT INTO fork_message_sources VALUES(?,?,?,?,?)", (org, copied, row["id"], row["inference_run_id"], model))
            db.execute(
                "INSERT INTO topic_forks VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id, org, topic_id, message_id, source["sequence"], user, client_id, name, now),
            )
            self.store._audit(db, org, user, "topic.fork", new_id)
            return self._view(db, org, new_id)

    @staticmethod
    def _view(db, org, topic):
        row = _row(db.execute("SELECT * FROM topics WHERE organization_id=? AND id=?", (org, topic)).fetchone())
        provenance = _row(
            db.execute(
                "SELECT source_topic_id,source_message_id,through_sequence,created_by,created_at FROM topic_forks WHERE topic_id=?",
                (topic,),
            ).fetchone()
        )
        return {"topic": row, "provenance": provenance}


def feedback_export(db, org, topic):
    """Use only after the export caller checks the current topic ACL."""
    fork = db.execute(
        "SELECT source_topic_id,source_message_id,through_sequence,created_by,created_at "
        "FROM topic_forks WHERE organization_id=? AND topic_id=?",
        (org, topic),
    ).fetchone()
    return {
        "fork": dict(fork) if fork else None,
        "message_sources": [
            dict(row)
            for row in db.execute(
                "SELECT f.message_id,f.source_message_id,f.source_run_id,f.source_model FROM fork_message_sources f "
                "JOIN messages m ON m.id=f.message_id WHERE f.organization_id=? AND m.topic_id=? ORDER BY m.sequence",
                (org, topic),
            )
        ],
        "reactions": [
            dict(row)
            for row in db.execute(
                "SELECT target_key,model,emoji,count(*) AS count FROM chat_reactions WHERE organization_id=? AND topic_id=? "
                "GROUP BY target_key,model,emoji ORDER BY target_key,emoji",
                (org, topic),
            )
        ],
    }
