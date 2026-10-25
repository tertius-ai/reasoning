"""One-to-one direct conversations between human members of one organization, or between a member and their own bot.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

Direct conversations live in their own tables, outside the project/channel/topic hierarchy, so no
topic, bot, webhook, search, knowledge, export or inference path can read them. Every read and
write here requires the caller to be one of the conversation's two participants; organization
owners and administrators have no other access. A bot takes part only in its owner's conversation with it:
its replies come from a bot run that belongs to that conversation (bot_runtime.enqueue_direct), not to a topic.
"""

import time
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Response
from pydantic import Field

from .reaction_emoji_schema import EMOJIS, MODEL_REPLY_EMOJIS
from .request_input import RequestInput
from .store import Store, StoreError, _id, _row

# One refusal for a recipient who blocked the sender or left the organization, so it does not reveal a block.
UNAVAILABLE = "direct messages to this person are unavailable"
SENDER_BLOCKED = "unblock this person to send them direct messages"


class DirectMessages:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _human_member(db, organization_id: str, user_id: str) -> bool:
        return db.execute(
            "SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id "
            "WHERE m.organization_id=? AND m.user_id=? AND u.account_kind='human'",
            (organization_id, user_id),
        ).fetchone() is not None

    @staticmethod
    def _own_bot(db, organization_id: str, user_id: str, other_id: str):
        """`other_id`'s bot row when it is the caller's own active bot in this organization."""
        row = db.execute(
            "SELECT * FROM bots WHERE id=? AND organization_id=? AND created_by=? AND revoked_at IS NULL AND expires_at>?",
            (other_id, organization_id, user_id, int(time.time())),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _blocked(db, organization_id: str, user_id: str, other_id: str) -> bool:
        return db.execute(
            "SELECT 1 FROM direct_blocks WHERE organization_id=? AND user_id=? AND blocked_user_id=?",
            (organization_id, user_id, other_id),
        ).fetchone() is not None

    @staticmethod
    def access(db, user_id: str, organization_id: str, conversation_id: str, *, write: bool = False):
        """Authorize a participant; anyone else, including organization administrators, receives 404."""
        role = Store._require(db, user_id, organization_id)
        row = db.execute(
            "SELECT * FROM direct_conversations WHERE organization_id=? AND id=? AND (first_user_id=? OR second_user_id=?)",
            (organization_id, conversation_id, user_id, user_id),
        ).fetchone()
        if row is None:
            raise StoreError(404, "not found")
        conversation = dict(row)
        conversation["other_user_id"] = (
            conversation["second_user_id"] if conversation["first_user_id"] == user_id else conversation["first_user_id"]
        )
        if write and role == "viewer":
            raise StoreError(403, "insufficient organization permissions")
        return role, conversation

    def _deliverable(self, db, organization_id: str, user_id: str, other_id: str) -> None:
        if self._own_bot(db, organization_id, user_id, other_id):
            return
        if self._blocked(db, organization_id, user_id, other_id):
            raise StoreError(403, SENDER_BLOCKED)
        if self._blocked(db, organization_id, other_id, user_id) or not self._human_member(db, organization_id, other_id):
            raise StoreError(403, UNAVAILABLE)

    def _summaries(self, db, user_id: str, organization_id: str, conversation_id: str | None = None) -> list[dict[str, Any]]:
        rows = db.execute(
            """SELECT c.id,c.created_by,c.created_at,
                CASE WHEN c.first_user_id=? THEN c.second_user_id ELSE c.first_user_id END AS other_user_id,
                COALESCE(s.last_read_sequence,0) AS last_read_sequence, COALESCE(s.muted,0) AS muted,
                (SELECT COALESCE(MAX(m.sequence),0) FROM direct_messages m
                    WHERE m.organization_id=c.organization_id AND m.conversation_id=c.id) AS last_message_sequence,
                (SELECT MAX(m.created_at) FROM direct_messages m
                    WHERE m.organization_id=c.organization_id AND m.conversation_id=c.id) AS last_message_at,
                (SELECT COUNT(*) FROM direct_messages m WHERE m.organization_id=c.organization_id AND m.conversation_id=c.id
                    AND m.author_id<>? AND m.sequence>COALESCE(s.last_read_sequence,0)) AS unread_count
            FROM direct_conversations c LEFT JOIN direct_conversation_state s
                ON s.organization_id=c.organization_id AND s.conversation_id=c.id AND s.user_id=?
            WHERE c.organization_id=? AND (c.first_user_id=? OR c.second_user_id=?)""",
            (user_id, user_id, user_id, organization_id, user_id, user_id),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if conversation_id is not None and item["id"] != conversation_id:
                continue
            # A conversation appears to its recipient only once it holds a message.
            if conversation_id is None and not item["last_message_sequence"] and item["created_by"] != user_id:
                continue
            other = db.execute("SELECT id,name,account_kind FROM users WHERE id=?", (item.pop("other_user_id"),)).fetchone()
            item.pop("created_by")
            bot = other["account_kind"] == "bot"
            item["other_user"] = {"id": other["id"], "name": other["name"], "kind": "bot" if bot else "human"}
            item["other_member"] = (bool(self._own_bot(db, organization_id, user_id, other["id"])) if bot
                                    else self._human_member(db, organization_id, other["id"]))
            item["blocked"] = self._blocked(db, organization_id, user_id, other["id"])
            item["muted"] = bool(item["muted"])
            result.append(item)
        result.sort(key=lambda item: (-(item["last_message_at"] or item["created_at"]), item["id"]))
        return result

    @staticmethod
    def _message(row) -> dict[str, Any]:
        return {**_row(row), "author_kind": "human", "reactions": {"counts": {}, "mine": [], "reactors": {}}}

    @staticmethod
    def _reactions(db, user_id, messages):
        by_id = {message["id"]: message for message in messages}
        if not by_id:
            return messages
        authors = {message["author_id"] for message in messages}
        bots = {row[0] for row in db.execute("SELECT id FROM users WHERE account_kind='bot' AND id IN ("
                                             + ",".join("?" for _ in authors) + ")", tuple(authors))}
        for message in messages:
            message["author_kind"] = "bot" if message["author_id"] in bots else "human"
        rows = db.execute(
            "SELECT r.message_id,r.user_id,r.emoji,u.name FROM direct_message_reactions r "
            "JOIN users u ON u.id=r.user_id WHERE r.message_id IN (" + ",".join("?" for _ in by_id) + ") ORDER BY r.user_id",
            tuple(by_id),
        )
        for row in rows:
            feedback = by_id[row["message_id"]]["reactions"]
            emoji = row["emoji"]
            feedback["counts"][emoji] = feedback["counts"].get(emoji, 0) + 1
            feedback["reactors"].setdefault(emoji, []).append({"id": row["user_id"], "name": row["name"]})
            if row["user_id"] == user_id:
                feedback["mine"].append(emoji)
        return messages

    def reaction(self, user_id, organization_id, conversation_id, message_id, emoji=None, active=None):
        if active is not None and (emoji not in EMOJIS or type(active) is not bool):
            raise StoreError(400, "invalid reaction")
        with self.store.transaction(write=active is not None) as db:
            _, conversation = self.access(db, user_id, organization_id, conversation_id, write=active is not None)
            message = self._message(db.execute(
                "SELECT * FROM direct_messages WHERE organization_id=? AND conversation_id=? AND id=?",
                (organization_id, conversation_id, message_id),
            ).fetchone())
            if active is not None:
                self._deliverable(db, organization_id, user_id, conversation["other_user_id"])
                if active and emoji not in MODEL_REPLY_EMOJIS and db.execute(
                        "SELECT 1 FROM users WHERE id=? AND account_kind='bot'", (message["author_id"],)).fetchone():
                    raise StoreError(400, "react to a model reply with thumbs up, thumbs down or the upside-down smile")
                if self.store._postgres:
                    self.store._postgres.lock(db.native, "direct-reaction:" + message_id)
                old = db.execute("SELECT emoji FROM direct_message_reactions WHERE message_id=? AND user_id=?",
                                 (message_id, user_id)).fetchone()
                changed = (active and (not old or old[0] != emoji)) or (not active and old and old[0] == emoji)
                if changed:
                    db.execute("DELETE FROM direct_message_reactions WHERE message_id=? AND user_id=?", (message_id, user_id))
                    if active:
                        db.execute("INSERT INTO direct_message_reactions(organization_id,message_id,user_id,emoji) VALUES(?,?,?,?)",
                                   (organization_id, message_id, user_id, emoji))
                    db.execute("INSERT INTO direct_reaction_changes(organization_id,conversation_id,message_id) VALUES(?,?,?)",
                               (organization_id, conversation_id, message_id))
            return self._reactions(db, user_id, [message])[0]["reactions"]

    def conversations(self, user_id: str, organization_id: str) -> list[dict[str, Any]]:
        with self.store.transaction() as db:
            Store._require(db, user_id, organization_id)
            return self._summaries(db, user_id, organization_id)

    def conversation(self, user_id: str, organization_id: str, conversation_id: str) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.access(db, user_id, organization_id, conversation_id)
            return self._summaries(db, user_id, organization_id, conversation_id)[0]

    def open(self, user_id: str, organization_id: str, other_id: str) -> tuple[dict[str, Any], bool]:
        """Return the caller's conversation with another human member or their own bot, creating it when allowed."""
        if other_id == user_id:
            raise StoreError(400, "choose another member")
        with self.store.transaction(write=True) as db:
            role = Store._require(db, user_id, organization_id)
            if not self._human_member(db, organization_id, other_id) and not self._own_bot(db, organization_id, user_id, other_id):
                raise StoreError(404, "not found")
            first, second = sorted((user_id, other_id))
            existing = db.execute(
                "SELECT id FROM direct_conversations WHERE organization_id=? AND first_user_id=? AND second_user_id=?",
                (organization_id, first, second),
            ).fetchone()
            if existing is not None:
                return self._summaries(db, user_id, organization_id, existing["id"])[0], False
            if role == "viewer":
                raise StoreError(403, "insufficient organization permissions")
            self._deliverable(db, organization_id, user_id, other_id)
            conversation_id = _id()
            db.execute(
                "INSERT INTO direct_conversations(id,organization_id,first_user_id,second_user_id,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (conversation_id, organization_id, first, second, user_id, int(time.time())),
            )
            return self._summaries(db, user_id, organization_id, conversation_id)[0], True

    def messages(self, user_id, organization_id, conversation_id, after=0, limit=100, before=None) -> list[dict[str, Any]]:
        if not 0 <= after <= 2**63 - 1 or not 1 <= limit <= 200 or (before is not None and (after != 0 or not 1 <= before <= 2**63 - 1)):
            raise StoreError(400, "invalid pagination")
        with self.store.transaction() as db:
            self.access(db, user_id, organization_id, conversation_id)
            rows = db.execute(
                "SELECT * FROM direct_messages WHERE organization_id=? AND conversation_id=? AND sequence"
                + ("<? ORDER BY sequence DESC LIMIT ?" if before is not None else ">? ORDER BY sequence LIMIT ?"),
                (organization_id, conversation_id, before if before is not None else after, limit),
            ).fetchall()
            result = self._reactions(db, user_id, [self._message(row) for row in rows])
            return list(reversed(result)) if before is not None else result

    def post(self, user_id, organization_id, conversation_id, content: str, client_id: str, reply_to: str | None = None,
             bot_model: str | None = None, effort: str | None = None) -> dict[str, Any]:
        """Post a message. In a conversation with your own bot it also queues the bot's answer, with that model and effort."""
        with self.store.transaction(write=True) as db:
            _, conversation = self.access(db, user_id, organization_id, conversation_id, write=True)
            existing = db.execute(
                "SELECT * FROM direct_messages WHERE organization_id=? AND author_id=? AND client_id=?",
                (organization_id, user_id, client_id),
            ).fetchone()
            if existing is not None:
                original = db.execute(
                    "SELECT content FROM direct_message_revisions WHERE message_id=? AND revision=0", (existing["id"],)
                ).fetchone()
                if (existing["conversation_id"] != conversation_id or (original[0] if original else existing["content"]) != content
                        or existing["reply_to"] != reply_to):
                    raise StoreError(409, "client_id was already used for a different message")
                return self._reactions(db, user_id, [self._message(existing)])[0]
            self._deliverable(db, organization_id, user_id, conversation["other_user_id"])
            # A reply names a message of this conversation only, so it never links or reveals another conversation.
            if reply_to is not None and not db.execute(
                "SELECT 1 FROM direct_messages WHERE organization_id=? AND conversation_id=? AND id=?",
                (organization_id, conversation_id, reply_to),
            ).fetchone():
                raise StoreError(400, "a reply must answer a message in the same conversation")
            message_id = _id()
            db.execute(
                "INSERT INTO direct_messages(id,organization_id,conversation_id,author_id,content,client_id,created_at,reply_to) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (message_id, organization_id, conversation_id, user_id, content, client_id, int(time.time()), reply_to),
            )
            message = self._message(db.execute("SELECT * FROM direct_messages WHERE id=?", (message_id,)).fetchone())
            # The author has read everything up to their own message.
            self._read(db, user_id, organization_id, conversation_id, message["sequence"])
            bot = self._own_bot(db, organization_id, user_id, conversation["other_user_id"])
            if bot is not None:
                from .bot_runtime import BotRuntime
                from .bot_store import BotStore

                BotRuntime(BotStore(self.store)).enqueue_direct(db, user_id, organization_id, conversation_id, bot, message,
                                                                 bot_model, effort)
            return self._reactions(db, user_id, [message])[0]

    def edit(self, user_id, organization_id, conversation_id, message_id, content, revision) -> dict[str, Any]:
        if not isinstance(content, str) or not content.strip() or len(content) > 16000 or type(revision) is not int or revision < 0:
            raise StoreError(400, "invalid message edit")
        with self.store.transaction(write=True) as db:
            _, conversation = self.access(db, user_id, organization_id, conversation_id, write=True)
            message = _row(db.execute(
                "SELECT * FROM direct_messages WHERE organization_id=? AND conversation_id=? AND id=?",
                (organization_id, conversation_id, message_id),
            ).fetchone())
            if message["author_id"] != user_id:
                raise StoreError(403, "only the author can edit this message")
            # Exact retries are safe, but a different stale draft cannot overwrite a newer edit.
            if message["revision"] == revision + 1 and message["content"] == content:
                return self._reactions(db, user_id, [self._message(message)])[0]
            if message["revision"] != revision:
                raise StoreError(409, "message changed; review the current version before saving")
            if content == message["content"]:
                return self._reactions(db, user_id, [self._message(message)])[0]
            self._deliverable(db, organization_id, user_id, conversation["other_user_id"])
            if revision == 0:
                db.execute(
                    "INSERT INTO direct_message_revisions(organization_id,message_id,revision,content,edited_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (organization_id, message_id, 0, message["content"], user_id, message["created_at"]),
                )
            now = int(time.time())
            db.execute(
                "INSERT INTO direct_message_revisions(organization_id,message_id,revision,content,edited_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (organization_id, message_id, revision + 1, content, user_id, now),
            )
            db.execute("UPDATE direct_messages SET content=?,revision=?,edited_at=? WHERE id=?", (content, revision + 1, now, message_id))
            return self._reactions(db, user_id, [self._message(
                db.execute("SELECT * FROM direct_messages WHERE id=?", (message_id,)).fetchone())])[0]

    def history(self, user_id, organization_id, conversation_id, message_id) -> list[dict[str, Any]]:
        with self.store.transaction() as db:
            self.access(db, user_id, organization_id, conversation_id)
            message = _row(db.execute(
                "SELECT * FROM direct_messages WHERE organization_id=? AND conversation_id=? AND id=?",
                (organization_id, conversation_id, message_id),
            ).fetchone())
            if not message["revision"]:
                return [{"revision": 0, "content": message["content"], "edited_by": message["author_id"],
                         "created_at": message["created_at"]}]
            return [dict(row) for row in db.execute(
                "SELECT revision,content,edited_by,created_at FROM direct_message_revisions "
                "WHERE organization_id=? AND message_id=? ORDER BY revision",
                (organization_id, message_id),
            )]

    def thread(self, user_id, organization_id, conversation_id, message_id) -> dict[str, Any]:
        """The thread containing `message_id`: its root and every reply below it, oldest first, at most THREAD_LIMIT."""
        from .message_replies import THREAD_LIMIT

        with self.store.transaction() as db:
            self.access(db, user_id, organization_id, conversation_id)
            target = "SELECT id,reply_to FROM direct_messages WHERE organization_id=? AND conversation_id=? AND id=?"
            root = db.execute(target, (organization_id, conversation_id, message_id)).fetchone()
            if root is None:
                raise StoreError(404, "message not found")
            seen = {root["id"]}
            while root["reply_to"] and len(seen) < THREAD_LIMIT:
                parent = db.execute(target, (organization_id, conversation_id, root["reply_to"])).fetchone()
                if parent is None or parent["id"] in seen:
                    break
                seen.add(parent["id"])
                root = parent
            ids, frontier, truncated = [root["id"]], [root["id"]], False
            while frontier:
                placeholders = ",".join("?" * len(frontier))
                children = [row["id"] for row in db.execute(
                    f"SELECT id FROM direct_messages WHERE organization_id=? AND conversation_id=? AND reply_to IN ({placeholders}) "
                    "ORDER BY sequence", (organization_id, conversation_id, *frontier))]
                children = [child for child in children if child not in ids]
                if len(ids) + len(children) > THREAD_LIMIT:
                    children, truncated = children[: THREAD_LIMIT - len(ids)], True
                ids.extend(children)
                frontier = children
            placeholders = ",".join("?" * len(ids))
            rows = db.execute(
                f"SELECT * FROM direct_messages WHERE organization_id=? AND conversation_id=? AND id IN ({placeholders}) ORDER BY sequence",
                (organization_id, conversation_id, *ids)).fetchall()
            messages = self._reactions(db, user_id, [self._message(row) for row in rows])
            return {"root_id": root["id"], "message_id": message_id, "messages": messages, "truncated": truncated}

    def updates(self, user_id, organization_id, conversation_id, after=0, limit=200, reaction_after=0) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.access(db, user_id, organization_id, conversation_id)
            changes = db.execute(
                "SELECT r.sequence,r.message_id FROM direct_message_revisions r JOIN direct_messages m ON m.id=r.message_id "
                "WHERE r.organization_id=? AND m.conversation_id=? AND r.sequence>? ORDER BY r.sequence LIMIT ?",
                (organization_id, conversation_id, after, limit),
            ).fetchall()
            reactions = db.execute(
                "SELECT sequence,message_id FROM direct_reaction_changes WHERE organization_id=? AND conversation_id=? "
                "AND sequence>? ORDER BY sequence LIMIT ?", (organization_id, conversation_id, reaction_after, limit),
            ).fetchall()
            messages = [
                self._message(db.execute("SELECT * FROM direct_messages WHERE id=?", (message_id,)).fetchone())
                for message_id in dict.fromkeys(row["message_id"] for row in [*changes, *reactions])
            ]
            return {"after": changes[-1]["sequence"] if changes else after,
                    "reaction_after": reactions[-1]["sequence"] if reactions else reaction_after,
                    "messages": self._reactions(db, user_id, messages)}

    def watermarks(self, user_id, organization_id, conversation_id) -> dict[str, int]:
        from .topic_events import direct_signature

        with self.store.transaction() as db:
            self.access(db, user_id, organization_id, conversation_id)
            value = direct_signature(db, organization_id, conversation_id)["messages"]
        return {"sequence": value[0], "updates": value[1], "reactions": value[2]}

    @staticmethod
    def _read(db, user_id, organization_id, conversation_id, sequence) -> None:
        db.execute(
            "INSERT OR IGNORE INTO direct_conversation_state(organization_id,conversation_id,user_id) VALUES(?,?,?)",
            (organization_id, conversation_id, user_id),
        )
        db.execute(
            "UPDATE direct_conversation_state SET last_read_sequence=MAX(last_read_sequence,?) "
            "WHERE organization_id=? AND conversation_id=? AND user_id=?",
            (sequence, organization_id, conversation_id, user_id),
        )

    def mark_unread(self, user_id, organization_id, conversation_id, message_id) -> dict[str, int]:
        """Move this participant's read point back to just before `message_id`; it never moves forward here."""
        with self.store.transaction(write=True) as db:
            self.access(db, user_id, organization_id, conversation_id)
            message = db.execute(
                "SELECT sequence FROM direct_messages WHERE organization_id=? AND conversation_id=? AND id=?",
                (organization_id, conversation_id, message_id),
            ).fetchone()
            if message is None:
                raise StoreError(404, "message not found")
            self._read(db, user_id, organization_id, conversation_id, 0)
            db.execute(
                "UPDATE direct_conversation_state SET last_read_sequence=MIN(last_read_sequence,?) "
                "WHERE organization_id=? AND conversation_id=? AND user_id=?",
                (message["sequence"] - 1, organization_id, conversation_id, user_id),
            )
            row = db.execute(
                "SELECT last_read_sequence FROM direct_conversation_state WHERE organization_id=? AND conversation_id=? AND user_id=?",
                (organization_id, conversation_id, user_id),
            ).fetchone()
            return {"last_read_sequence": row["last_read_sequence"]}

    def preference(self, user_id, organization_id, conversation_id, *, muted: bool | None = None, sequence: int | None = None) -> None:
        with self.store.transaction(write=True) as db:
            self.access(db, user_id, organization_id, conversation_id)
            if sequence is not None and sequence != 0 and db.execute(
                "SELECT 1 FROM direct_messages WHERE organization_id=? AND conversation_id=? AND sequence=?",
                (organization_id, conversation_id, sequence),
            ).fetchone() is None:
                raise StoreError(400, "read cursor must identify a message in this conversation")
            self._read(db, user_id, organization_id, conversation_id, sequence or 0)
            if muted is not None:
                db.execute(
                    "UPDATE direct_conversation_state SET muted=? WHERE organization_id=? AND conversation_id=? AND user_id=?",
                    (int(muted), organization_id, conversation_id, user_id),
                )

    def blocks(self, user_id: str, organization_id: str) -> list[dict[str, Any]]:
        with self.store.transaction() as db:
            Store._require(db, user_id, organization_id)
            return [dict(row) for row in db.execute(
                "SELECT u.id,u.name,b.created_at FROM direct_blocks b JOIN users u ON u.id=b.blocked_user_id "
                "WHERE b.organization_id=? AND b.user_id=? ORDER BY u.name COLLATE NOCASE,u.id",
                (organization_id, user_id),
            )]

    def block(self, user_id: str, organization_id: str, other_id: str, blocked: bool) -> None:
        """Personal safety preference; viewers can block too. Nobody else is told about a block."""
        if other_id == user_id:
            raise StoreError(400, "choose another member")
        with self.store.transaction(write=True) as db:
            Store._require(db, user_id, organization_id)
            if not blocked:
                db.execute(
                    "DELETE FROM direct_blocks WHERE organization_id=? AND user_id=? AND blocked_user_id=?",
                    (organization_id, user_id, other_id),
                )
                return
            if not self._human_member(db, organization_id, other_id):
                raise StoreError(404, "not found")
            db.execute(
                "INSERT OR IGNORE INTO direct_blocks(organization_id,user_id,blocked_user_id,created_at) VALUES(?,?,?,?)",
                (organization_id, user_id, other_id, int(time.time())),
            )


class Recipient(RequestInput):
    user_id: Annotated[str, Field(min_length=1, max_length=64)]


class DirectMessage(RequestInput):
    content: Annotated[str, Field(min_length=1, max_length=16000, pattern=r"\S")]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]
    reply_to: Annotated[str | None, Field(pattern=r"^[a-f0-9]{32}$")] = None
    # In a conversation with your own bot: which of its models answers, and with which effort.
    bot_model: Annotated[str | None, Field(min_length=1, max_length=100)] = None
    effort: Annotated[str | None, Field(min_length=1, max_length=16)] = None


class DirectMessageEdit(RequestInput):
    content: Annotated[str, Field(min_length=1, max_length=16000, pattern=r"\S")]
    revision: Annotated[int, Field(ge=0)]


class DirectReadCursor(RequestInput):
    sequence: Annotated[int, Field(ge=0, le=2**63 - 1)]


class DirectUnreadFrom(RequestInput):
    message_id: Annotated[str, Field(min_length=1, max_length=64)]


class DirectMute(RequestInput):
    muted: bool


class DirectReaction(RequestInput):
    emoji: Annotated[str, Field(min_length=1, max_length=32)]
    active: bool


def install_routes(app: FastAPI, store: Store, authenticated: Callable) -> None:
    User = Annotated[str, Depends(authenticated)]
    direct = DirectMessages(store)
    prefix = "/api/organizations/{organization_id}"
    conversation = prefix + "/direct-conversations/{conversation_id}"

    @app.get(prefix + "/direct-conversations")
    def conversations(organization_id: str, user: User):
        return direct.conversations(user, organization_id)

    @app.post(prefix + "/direct-conversations", status_code=201)
    def open_conversation(organization_id: str, body: Recipient, user: User, response: Response):
        result, created = direct.open(user, organization_id, body.user_id)
        if not created:
            response.status_code = 200
        return result

    @app.get(conversation)
    def read_conversation(organization_id: str, conversation_id: str, user: User):
        return direct.conversation(user, organization_id, conversation_id)

    @app.get(conversation + "/messages")
    def messages(
        organization_id: str,
        conversation_id: str,
        user: User,
        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        before: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    ):
        return direct.messages(user, organization_id, conversation_id, after, limit, before)

    @app.post(conversation + "/messages", status_code=201)
    def post_message(organization_id: str, conversation_id: str, body: DirectMessage, user: User):
        return direct.post(user, organization_id, conversation_id, body.content, body.client_id, body.reply_to, body.bot_model, body.effort)

    @app.get(conversation + "/bot-runs")
    def bot_runs(organization_id: str, conversation_id: str, user: User):
        from .bot_runtime import BotRuntime
        from .bot_store import BotStore

        return BotRuntime(BotStore(store)).direct_listing(user, organization_id, conversation_id)

    @app.get(conversation + "/messages/{message_id}/thread")
    def message_thread(organization_id: str, conversation_id: str, message_id: str, user: User):
        return direct.thread(user, organization_id, conversation_id, message_id)

    @app.get(conversation + "/message-updates")
    def message_updates(organization_id: str, conversation_id: str, user: User,
                        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
                        reaction_after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0):
        return direct.updates(user, organization_id, conversation_id, after, reaction_after=reaction_after)

    @app.get(conversation + "/messages/{message_id}/reactions")
    def read_reactions(organization_id: str, conversation_id: str, message_id: str, user: User):
        return direct.reaction(user, organization_id, conversation_id, message_id)

    @app.put(conversation + "/messages/{message_id}/reactions")
    def write_reaction(organization_id: str, conversation_id: str, message_id: str, body: DirectReaction, user: User):
        return direct.reaction(user, organization_id, conversation_id, message_id, body.emoji, body.active)

    @app.patch(conversation + "/messages/{message_id}")
    def edit_message(organization_id: str, conversation_id: str, message_id: str, body: DirectMessageEdit, user: User):
        return direct.edit(user, organization_id, conversation_id, message_id, body.content, body.revision)

    @app.get(conversation + "/messages/{message_id}/history")
    def message_history(organization_id: str, conversation_id: str, message_id: str, user: User):
        return direct.history(user, organization_id, conversation_id, message_id)

    @app.put(conversation + "/read", status_code=204)
    def read_cursor(organization_id: str, conversation_id: str, body: DirectReadCursor, user: User):
        direct.preference(user, organization_id, conversation_id, sequence=body.sequence)

    @app.put(conversation + "/unread")
    def unread_from(organization_id: str, conversation_id: str, body: DirectUnreadFrom, user: User):
        return direct.mark_unread(user, organization_id, conversation_id, body.message_id)

    @app.put(conversation + "/mute", status_code=204)
    def mute(organization_id: str, conversation_id: str, body: DirectMute, user: User):
        direct.preference(user, organization_id, conversation_id, muted=body.muted)

    @app.get(prefix + "/direct-blocks")
    def blocks(organization_id: str, user: User):
        return direct.blocks(user, organization_id)

    @app.put(prefix + "/direct-blocks/{member_id}", status_code=204)
    def block(organization_id: str, member_id: str, user: User):
        direct.block(user, organization_id, member_id, True)

    @app.delete(prefix + "/direct-blocks/{member_id}", status_code=204)
    def unblock(organization_id: str, member_id: str, user: User):
        direct.block(user, organization_id, member_id, False)
