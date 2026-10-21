"""Reply threads: a message, the message it answers, and every reply below them in one topic.

A reply names its target through `messages.reply_to`, which `Store._post_message` only accepts for a
message of the same topic. A thread is read with the topic's own access check, so it shows nothing a
reader could not already page through.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from typing import Annotated

from fastapi import Depends

from .store import Store, StoreError

# One thread returns at most this many messages; `truncated` reports when replies were left out.
THREAD_LIMIT = 200


def message_thread(store: Store, user: str, org: str, topic_id: str, message_id: str) -> dict:
    """The thread containing `message_id`: its root (the first message without a reply target) and all replies, oldest first."""
    with store.transaction() as db:
        store._topic_access(db, user, org, topic_id)
        target = "SELECT id,reply_to FROM messages WHERE organization_id=? AND topic_id=? AND id=?"
        root = db.execute(target, (org, topic_id, message_id)).fetchone()
        if root is None:
            raise StoreError(404, "message not found")
        seen = {root["id"]}
        while root["reply_to"] and len(seen) < THREAD_LIMIT:
            parent = db.execute(target, (org, topic_id, root["reply_to"])).fetchone()
            if parent is None or parent["id"] in seen:
                break
            seen.add(parent["id"])
            root = parent
        ids, frontier, truncated = [root["id"]], [root["id"]], False
        while frontier:
            placeholders = ",".join("?" * len(frontier))
            children = [row["id"] for row in db.execute(
                f"SELECT id FROM messages WHERE organization_id=? AND topic_id=? AND reply_to IN ({placeholders}) ORDER BY sequence",
                (org, topic_id, *frontier))]
            children = [child for child in children if child not in ids]
            if len(ids) + len(children) > THREAD_LIMIT:
                children, truncated = children[: THREAD_LIMIT - len(ids)], True
            ids.extend(children)
            frontier = children
        placeholders = ",".join("?" * len(ids))
        rows = db.execute(
            "SELECT m.*,u.name AS sender_name FROM messages m JOIN users u ON u.id=m.author_id "
            f"WHERE m.organization_id=? AND m.topic_id=? AND m.id IN ({placeholders}) ORDER BY m.sequence",
            (org, topic_id, *ids))
        return {"root_id": root["id"], "message_id": message_id, "messages": [store._message(row, db) for row in rows],
                "truncated": truncated}


def install_routes(app, store: Store, authenticated) -> None:
    User = Annotated[str, Depends(authenticated)]

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/messages/{message_id}/thread")
    def thread(organization_id: str, topic_id: str, message_id: str, user: User):
        return message_thread(store, user, organization_id, topic_id, message_id)
