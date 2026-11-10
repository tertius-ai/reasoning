"""Private side chats: one member's own conversation with a model, beside a topic and reading its context.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

A side chat belongs to one member and one AI chat (beta) topic. Its messages live in ``side_chat_messages``,
outside the topic, so no topic, search, webhook, export or knowledge path reads them. Every read and write here
is the caller's own side chat; nobody else, organization administrators included, can open it.

A reply is an ordinary model run of the topic, so it reads the topic's shared context, and the ledger statements
it records belong to the topic as usual. ``side_chat_runs`` marks the run private: run listings, live updates,
research views and exports leave it out, only its requester can open or cancel it, and its answer is stored here
instead of being posted to the topic. Its tools are limited to reading and to ledger writes (``SIDE_CHAT_TOOLS``).

Bringing replies to the topic posts one message from the owner: their own words, if any, then the chosen side
messages in their original order, which the conversation shows as a folded group. The post may answer a topic
message. A side message reaches the topic once (``side_chat_brought_messages``). The chosen replies read the side
chat before their questions; the post links to earlier posts that carried any of that and counts the rest, which
stays private.
"""

import json
import time
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import Depends, Query
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .request_input import RequestInput
from .store import StoreError, _id, _row

# Tools a side-chat reply may use: reading the topic, its files, the ledger and outside sources, and recording
# ledger statements and references. Nothing else it could write is private to the side chat, so other tools are
# left out; a tool added later stays out until it is listed here.
SIDE_CHAT_TOOLS = frozenset({
    "answer_checkpoint", "topic_history", "topic_message_read", "topic_search", "tool_receipt_read",
    "knowledge_search", "knowledge_read", "artifact_read", "artifact_list", "research_file_read",
    "ledger_status", "ledger_search", "ledger_graph", "ledger_blockers", "ledger_blast_radius",
    "ledger_references", "ledger_search_references", "ledger_read", "ledger_reviews",
    "ledger_claim", "ledger_refine", "ledger_prove", "ledger_revise_proof", "ledger_reference", "ledger_refine_reference",
    "bibliography_get", "bibliography_search", "bibliography_add", "account_read",
    "workflow_list", "workflow_result", "workflow_read", "compute_catalog", "compute_result", "compute_log_read",
    "model_catalog", "blog_read", "blog_search",
    "crossref_search", "crossref_lookup", "arxiv_search", "arxiv_lookup", "arxiv_read", "arxiv_source_list", "arxiv_source_read",
    "web_fetch", "web_search", "math_search", "math_read", "lmfdb_catalog", "lmfdb_schema", "lmfdb_query", "media_read",
    "scientific_calculate", "unit_convert", "constant_lookup",
})
# The newest side messages returned at once, and the most messages one group brings to the topic.
PAGE_LIMIT = 200
BRING_LIMIT = 50
# A brought group longer than this, your words included, is refused; choose fewer exchanges.
BRING_CHARACTERS = 64_000
# Your own words above a brought group.
WORDS_LIMIT = 4000
# The most of a topic message's text a side question about it passes to the model.
ABOUT_CHARACTERS = 8000
ALREADY_BROUGHT = "some of these side messages are already in the topic; a new post links to them instead"
INSTRUCTION = (
    "Respond to the latest side-chat message, privately, for the side chat's owner. The topic's shared conversation "
    "above is context. Ledger statements you record belong to the topic as usual; do not otherwise repeat the side chat. "
    "Source messages are JSON records for reference; reply in plain Markdown, never as such a record."
)


def public_request(column: str) -> str:
    """SQL that holds when the model request in ``column`` is not a private side-chat reply."""
    return f"NOT EXISTS (SELECT 1 FROM side_chat_runs sc WHERE sc.request_id={column})"


def public_run(column: str) -> str:
    """SQL that holds when the model run in ``column`` is not a private side-chat reply."""
    return f"NOT EXISTS (SELECT 1 FROM llm_runs sr JOIN side_chat_runs sc ON sc.request_id=sr.request_id WHERE sr.id={column})"


def private_owner(db, request_id: str) -> str | None:
    row = db.execute("SELECT owner_id FROM side_chat_runs WHERE request_id=?", (request_id,)).fetchone()
    return row[0] if row else None


def require_visible(db, user: str, run: dict) -> None:
    """A private side-chat run exists only for its requester; anyone else, administrators included, gets 404."""
    owner = private_owner(db, run["request_id"])
    if owner is not None and owner != user:
        raise StoreError(404, "not found")


def message_href(org: str, topic: str, message: str) -> str:
    """The application's link to a topic message, as the web client builds it."""
    return "/organization.html?" + urlencode({"organization": org, "topic": topic, "message": message})


def _additions(db, org: str, run_id: str, *, read: bool = False) -> list[dict]:
    """Side messages added to a side reply, shaped as a topic reply's additions; ``read`` keeps those the reply read."""
    rows = db.execute(
        "SELECT id,owner_id,content,created_at,addition_read_at,addition_read_step FROM side_chat_messages "
        "WHERE organization_id=? AND adds_to_run=?" + (" AND addition_read_at IS NOT NULL" if read else "") + " ORDER BY sequence",
        (org, run_id),
    ).fetchall()
    return [{"message_id": row["id"], "run_id": run_id, "added_by": row["owner_id"], "moved": False, "cancelled_runs": [],
             "created_at": row["created_at"], "read_at": row["addition_read_at"], "read_step": row["addition_read_step"],
             "content": row["content"]} for row in rows]


def take_additions(db, inference, run, step: int) -> list[dict]:
    """The side messages added to side reply ``run`` that it has not read, marked read at ``step``, oldest first, as the
    side chat's records in its conversation. A topic reply has none."""
    rows = db.execute(
        "SELECT m.id,m.sequence,m.content,u.name FROM side_chat_messages m JOIN users u ON u.id=m.owner_id "
        "WHERE m.organization_id=? AND m.adds_to_run=? AND m.addition_read_at IS NULL ORDER BY m.sequence",
        (run["organization_id"], run["id"]),
    ).fetchall()
    if not rows:
        return []
    now = int(time.time())
    db.executemany("UPDATE side_chat_messages SET addition_read_at=?,addition_read_step=? WHERE id=?",
                   [(now, step, row["id"]) for row in rows])
    inference._event(db, run["organization_id"], run["id"], {"type": "agent", "phase": "additions_read", "step": step,
                                                             "message_ids": [row["id"] for row in rows]})
    return [{"id": row["id"], "side_sequence": row["sequence"], "kind": "human", "text": row["content"], "sender": row["name"]}
            for row in rows]


class SideChats:
    def __init__(self, inference):
        self.inference, self.store = inference, inference.store

    def _access(self, db, user: str, org: str, topic: str, *, write: bool = False) -> dict:
        target = self.store._topic_access(db, user, org, topic, write=write)[4]
        if target["kind"] != "beta":
            raise StoreError(400, "side chats belong to AI chat topics")
        return target

    @staticmethod
    def _message(db, row) -> dict:
        """A side message; an answer carries its run's steps and usage, as a topic reply does, and each message names
        the topic post that brought it, if one did."""
        from .reply_steps import run_activity
        from .usage_store import footprint

        result = _row(row)
        result["author_kind"] = result.pop("sender_kind")
        about = result.pop("about_message_id", None)
        if about:
            # A question about a topic message names it, its author and its time, for a link back to it.
            found = db.execute("SELECT m.id,m.created_at,m.author_id,u.name FROM messages m LEFT JOIN users u ON u.id=m.author_id "
                               "WHERE m.organization_id=? AND m.id=?", (result["organization_id"], about)).fetchone()
            result["about"] = ({"message_id": found[0], "created_at": found[1], "author_id": found[2], "author_name": found[3]}
                               if found else None)
        # A message added to a side reply at work names the run, and when and at which step the reply read it.
        added_to, read_at, read_step = (result.pop(key, None) for key in ("adds_to_run", "addition_read_at", "addition_read_step"))
        result["addition"] = {"run_id": added_to, "read_at": read_at, "read_step": read_step} if added_to else None
        org, run_id = result["organization_id"], result["inference_run_id"]
        if result["author_kind"] == "assistant" and run_id:
            run = db.execute("SELECT prompt_tokens,completion_tokens,actual_microusd,impact_profile_json FROM llm_runs "
                             "WHERE organization_id=? AND id=?", (org, run_id)).fetchone()
            result["usage"] = footprint(run) if run is not None else None
            steps = run_activity(db, org, run_id)
            if steps:
                result["steps"] = steps
            additions = _additions(db, org, run_id, read=True)
            if additions:
                result["additions"] = additions
        post = db.execute(
            "SELECT b.message_id,m.created_at FROM side_chat_brought_messages b JOIN messages m ON m.id=b.message_id "
            "WHERE b.side_message_id=?",
            (result["id"],),
        ).fetchone()
        result["brought"] = {"message_id": post["message_id"], "created_at": post["created_at"]} if post else None
        return result

    def _runs(self, db, user: str, org: str, topic: str) -> list[dict]:
        rows = db.execute(
            self.inference._joined() + "JOIN side_chat_runs s ON s.request_id=q.id "
            "WHERE r.organization_id=? AND s.topic_id=? AND s.owner_id=? ORDER BY q.created_at DESC,r.id DESC LIMIT 10",
            (org, topic, user),
        ).fetchall()
        runs = []
        for row in rows:
            view = self.inference._view(row, db)
            question = db.execute("SELECT question_id FROM side_chat_runs WHERE request_id=?", (row["request_id"],)).fetchone()
            view["side_chat"] = {"question_id": question[0]}
            view["additions"] = _additions(db, org, row["id"])
            runs.append(view)
        return runs

    def view(self, user: str, org: str, topic: str, after: int = 0) -> dict:
        """The caller's side chat: its newest messages (or those after ``after``) and its latest replies' runs."""
        with self.store.transaction() as db:
            self._access(db, user, org, topic)
            if after:
                rows = db.execute(
                    "SELECT * FROM side_chat_messages WHERE organization_id=? AND topic_id=? AND owner_id=? AND sequence>? "
                    "ORDER BY sequence LIMIT ?",
                    (org, topic, user, after, PAGE_LIMIT),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM side_chat_messages WHERE organization_id=? AND topic_id=? AND owner_id=? "
                    "ORDER BY sequence DESC LIMIT ?",
                    (org, topic, user, PAGE_LIMIT),
                ).fetchall()[::-1]
            return {"messages": [self._message(db, row) for row in rows], "runs": self._runs(db, user, org, topic)}

    def post(self, user: str, org: str, topic: str, content: str, client_id: str, about: str | None = None,
             adds_to: str | None = None) -> dict:
        """Add a question to the caller's side chat, optionally about a message of the topic, such as a brought post. With
        ``adds_to``, the message instead joins that side reply at work, which reads it before its next provider call; it asks
        for no reply of its own."""
        if not isinstance(content, str) or not content.strip() or len(content) > 16000 or not client_id or len(client_id) > 128:
            raise StoreError(400, "a side message needs text of at most 16000 characters and a client_id")
        if adds_to is not None and about is not None:
            raise StoreError(400, "a message added to a reply is not about a post")
        with self.store.transaction(write=True) as db:
            self._access(db, user, org, topic, write=True)
            existing = db.execute(
                "SELECT * FROM side_chat_messages WHERE organization_id=? AND owner_id=? AND client_id=?", (org, user, client_id)
            ).fetchone()
            if existing:
                if (existing["topic_id"] != topic or existing["content"] != content or existing["sender_kind"] != "human"
                        or existing["about_message_id"] != about or existing["adds_to_run"] != adds_to):
                    raise StoreError(409, "client_id was already used for a different side message")
                return self._message(db, existing)
            if about is not None and not db.execute("SELECT 1 FROM messages WHERE organization_id=? AND topic_id=? AND id=?",
                                                    (org, topic, about)).fetchone():
                raise StoreError(404, "message not found in this topic")
            if adds_to is not None:
                self._at_work(db, user, org, topic, adds_to)
            message_id = _id()
            db.execute(
                "INSERT INTO side_chat_messages(id,organization_id,topic_id,owner_id,sender_kind,content,client_id,created_at,"
                "about_message_id,adds_to_run) VALUES(?,?,?,?,'human',?,?,?,?,?)",
                (message_id, org, topic, user, content, client_id, int(time.time()), about, adds_to),
            )
            if adds_to is not None:
                self.inference._event(db, org, adds_to, {"type": "addition", "message_id": message_id, "moved": False})
            return self._message(db, db.execute("SELECT * FROM side_chat_messages WHERE id=?", (message_id,)).fetchone())

    @staticmethod
    def _at_work(db, user: str, org: str, topic: str, run_id: str) -> None:
        """The caller's side reply in this topic that a new side message can join: it must still be at work."""
        row = db.execute(
            "SELECT r.status,r.cancel_requested FROM llm_runs r JOIN side_chat_runs s ON s.request_id=r.request_id "
            "WHERE r.organization_id=? AND r.id=? AND s.topic_id=? AND s.owner_id=?",
            (org, run_id, topic, user),
        ).fetchone()
        if row is None:
            raise StoreError(404, "side reply not found")
        if row["status"] not in ("queued", "running") or row["cancel_requested"]:
            raise StoreError(409, "the model reply has finished; send this as a new question")

    def remove_addition(self, user: str, org: str, topic: str, message_id: str) -> dict:
        """Take back a side message its reply has not read yet; it stays in the side chat as a message of its own."""
        with self.store.transaction(write=True) as db:
            self._access(db, user, org, topic, write=True)
            row = db.execute(
                "SELECT * FROM side_chat_messages WHERE organization_id=? AND topic_id=? AND owner_id=? AND id=?",
                (org, topic, user, message_id),
            ).fetchone()
            if row is None or row["adds_to_run"] is None:
                raise StoreError(404, "addition not found")
            if row["addition_read_at"] is not None:
                raise StoreError(409, "the reply has already read this message")
            db.execute("UPDATE side_chat_messages SET adds_to_run=NULL WHERE id=?", (message_id,))
            self.inference._event(db, org, row["adds_to_run"], {"type": "addition_removed", "message_id": message_id})
            return self._message(db, db.execute("SELECT * FROM side_chat_messages WHERE id=?", (message_id,)).fetchone())

    def _replayed(self, db, user: str, org: str, topic: str, client: str, parameters: dict, question: str):
        request = db.execute(
            "SELECT q.*,s.question_id FROM llm_requests q LEFT JOIN side_chat_runs s ON s.request_id=q.id "
            "WHERE q.organization_id=? AND q.requested_by=? AND q.client_id=?",
            (org, user, client),
        ).fetchone()
        if request is None:
            return None
        if request["topic_id"] != topic or request["question_id"] != question or json.loads(request["parameters_json"]) != parameters:
            raise StoreError(409, "client_id was already used for a different model request")
        return self._request_views(db, request["id"], question)

    def _request_views(self, db, request_id: str, question: str) -> list[dict]:
        return [view | {"side_chat": {"question_id": question}} for view in self.inference._request_runs(db, request_id)]

    def admit(self, user: str, org: str, topic: str, client: str, parameters: dict, question: str) -> list[dict] | None:
        """Replay lookup, then admission checks, before the provider is asked for quotes."""
        with self.store.transaction() as db:
            target = self._access(db, user, org, topic, write=True)
            existing = self._replayed(db, user, org, topic, client, parameters, question)
            if existing is None:
                self._question(db, user, org, topic, question)
                self.inference._preflight(db, user, org, topic, target=target)
            return existing

    @staticmethod
    def _question(db, user: str, org: str, topic: str, question: str):
        row = db.execute(
            "SELECT * FROM side_chat_messages WHERE organization_id=? AND topic_id=? AND owner_id=? AND id=?", (org, topic, user, question)
        ).fetchone()
        if row is None or row["sender_kind"] != "human":
            raise StoreError(404, "side message not found")
        return row

    def _context(self, db, user: str, org: str, topic: str, question, quotes) -> tuple[list[dict], dict, int]:
        """The topic's compiled context with the side chat, newest first within its budget, after it."""
        from .research_context import ResearchContext, encoded, token_bound

        through = db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM messages WHERE organization_id=? AND topic_id=?", (org, topic)
        ).fetchone()[0]
        messages, snapshot = ResearchContext(self.store).compile(db, org, topic, through, quotes, user=user)
        owner = db.execute("SELECT name FROM users WHERE id=?", (user,)).fetchone()[0]
        shared = messages[:-1]
        window = min(quote.context_length for quote in quotes)
        used = token_bound(shared)
        budget = min(window - 512, used + max(1024, snapshot["target_input_tokens"] // 2))
        side: list[dict] = []
        for row in db.execute(
            "SELECT * FROM side_chat_messages WHERE organization_id=? AND topic_id=? AND owner_id=? AND sequence<=? "
            "ORDER BY sequence DESC LIMIT ?",
            (org, topic, user, question["sequence"], PAGE_LIMIT),
        ):
            record = {"id": row["id"], "side_sequence": row["sequence"], "kind": row["sender_kind"], "text": row["content"],
                      "sender": row["assistant_model"] if row["sender_kind"] == "assistant" else owner}
            if row["sender_kind"] == "assistant":
                metadata = {key: value for key, value in record.items() if key != "text"}
                turns = [{"role": "system", "content": "Next side-chat assistant turn (reference data): " + encoded(metadata)},
                         {"role": "assistant", "content": row["content"]}]
            else:
                turns = [{"role": "user", "content": encoded(record)}]
            size = sum(token_bound(turn) for turn in turns)
            if side and used + size > budget:
                break
            if used + size > window - 512:
                raise StoreError(400, "the side message exceeds this model's context capacity")
            side.extend(reversed(turns))
            used += size
        side.reverse()
        regarding = self._regarding(db, org, topic, question)
        compiled = [
            *shared,
            {"role": "system", "content": "The topic's shared conversation ends here. What follows is the private side chat of "
             + encoded(owner) + " about this topic; only they see it."},
            *side,
            *regarding,
            {"role": "user", "content": INSTRUCTION},
        ]
        snapshot = {
            **snapshot,
            "messages": compiled,
            "side_chat": True,
            "side_question_id": question["id"],
            "through_sequence": 0,
            "memory_through_sequence": 0,
            "compact_through_sequence": 0,
            "input_token_upper_bound": token_bound(compiled),
        }
        return compiled, snapshot, through

    @staticmethod
    def _regarding(db, org: str, topic: str, question) -> list[dict]:
        """A note naming the topic message the latest side question is about, with its text, when it names one."""
        from .research_context import encoded

        if not question["about_message_id"]:
            return []
        row = db.execute("SELECT m.id,m.created_at,m.content,u.name FROM messages m LEFT JOIN users u ON u.id=m.author_id "
                         "WHERE m.organization_id=? AND m.topic_id=? AND m.id=?", (org, topic, question["about_message_id"])).fetchone()
        if row is None:
            return []
        record = {"id": row[0], "author": row[3], "created_at": row[1], "text": row[2][:ABOUT_CHARACTERS]}
        return [{"role": "system", "content": "The latest side question is about this message of the topic's conversation "
                 "(reference data): " + encoded(record)}]

    def enqueue(self, user: str, org: str, topic: str, client: str, parameters: dict, quotes, question: str) -> list[dict]:
        with self.store.transaction(write=True) as db:
            self._access(db, user, org, topic, write=True)
            existing = self._replayed(db, user, org, topic, client, parameters, question)
            if existing is not None:
                return existing
            row = self._question(db, user, org, topic, question)
            messages, snapshot, through = self._context(db, user, org, topic, row, quotes)
            runs = self.inference._enqueue(db, user, org, topic, client, parameters, quotes, research_override=(messages, snapshot))
            request_id = runs[0]["request_id"]
            db.execute(
                "INSERT INTO side_chat_runs(request_id,organization_id,topic_id,owner_id,question_id,through_sequence) "
                "VALUES(?,?,?,?,?,?)",
                (request_id, org, topic, user, question, through),
            )
            return self._request_views(db, request_id, question)

    @staticmethod
    def _speaker(row, owner: str) -> str:
        return row["assistant_model"] if row["sender_kind"] == "assistant" else owner

    def bring(self, user: str, org: str, topic: str, message_ids: list[str], words: str, reply_to: str | None,
              client_id: str) -> dict:
        """Post your words and the chosen side messages to the topic as one message from you, shown as a folded group.

        A side message reaches the topic once; asking again for one that is already there is refused. ``reply_to``
        makes the post answer a message of the topic.
        """
        if not 1 <= len(message_ids) <= BRING_LIMIT or len(set(message_ids)) != len(message_ids):
            raise StoreError(400, f"choose between one and {BRING_LIMIT} distinct side messages")
        words = words.strip()
        if len(words) > WORDS_LIMIT:
            raise StoreError(400, f"your words above brought side messages are limited to {WORDS_LIMIT} characters")
        with self.store.transaction(write=True) as db:
            self._access(db, user, org, topic, write=True)
            replayed = self._replayed_bring(db, user, org, topic, message_ids, words, reply_to, client_id)
            if replayed is not None:
                return replayed
            placeholders = ",".join("?" * len(message_ids))
            rows = db.execute(
                f"SELECT * FROM side_chat_messages WHERE organization_id=? AND topic_id=? AND owner_id=? AND id IN ({placeholders}) "
                "ORDER BY sequence",
                (org, topic, user, *message_ids),
            ).fetchall()
            if len(rows) != len(message_ids):
                raise StoreError(404, "side message not found")
            if not any(row["sender_kind"] == "assistant" for row in rows):
                raise StoreError(400, "bring at least one model reply")
            if db.execute(f"SELECT 1 FROM side_chat_brought_messages WHERE side_message_id IN ({placeholders})",
                          tuple(message_ids)).fetchone():
                raise StoreError(409, ALREADY_BROUGHT)
            # What the chosen replies read and the post leaves out: earlier posts are linked, the rest only counted.
            bound = self._read_before(db, rows)
            linked = [row[0] for row in db.execute(
                "SELECT DISTINCT b.message_id,m.sequence FROM side_chat_brought_messages b "
                "JOIN side_chat_messages s ON s.id=b.side_message_id JOIN messages m ON m.id=b.message_id "
                "WHERE s.organization_id=? AND s.topic_id=? AND s.owner_id=? AND s.sequence<? ORDER BY m.sequence",
                (org, topic, user, bound),
            )]
            omitted = db.execute(
                "SELECT COUNT(*) FROM side_chat_messages s WHERE s.organization_id=? AND s.topic_id=? AND s.owner_id=? "
                f"AND s.sequence<? AND s.id NOT IN ({placeholders}) "
                "AND NOT EXISTS (SELECT 1 FROM side_chat_brought_messages b WHERE b.side_message_id=s.id)",
                (org, topic, user, bound, *message_ids),
            ).fetchone()[0]
            owner = db.execute("SELECT name FROM users WHERE id=?", (user,)).fetchone()[0]
            content = self._brought_text(org, topic, rows, owner, words, linked, omitted)
            if len(content) > BRING_CHARACTERS:
                raise StoreError(413, "these side messages are too long to bring at once; choose fewer exchanges")
            message = self.store._post_message(db, user, org, topic, content, client_id, reply_to=reply_to)
            db.execute(
                "INSERT INTO side_chat_brought(message_id,organization_id,topic_id,owner_id,words,linked_message_ids_json,"
                "omitted_count,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (message["id"], org, topic, user, words, json.dumps(linked), omitted, int(time.time())),
            )
            for row in rows:
                inserted = db.execute(
                    "INSERT INTO side_chat_brought_messages(side_message_id,message_id,organization_id,owner_id) "
                    "VALUES(?,?,?,?) ON CONFLICT(side_message_id) DO NOTHING",
                    (row["id"], message["id"], org, user),
                )
                # A concurrent request brought it first; refusing undoes this whole post.
                if inserted.rowcount == 0:
                    raise StoreError(409, ALREADY_BROUGHT)
            self.store._audit(db, org, user, "side_chat.bring", message["id"])
            return self._posted(db, message["id"])

    def _replayed_bring(self, db, user: str, org: str, topic: str, message_ids: list[str], words: str, reply_to: str | None,
                        client_id: str) -> dict | None:
        """The post a repeated request made, or None for a new request; a client_id used for anything else is refused."""
        existing = db.execute(
            "SELECT m.id,m.topic_id,m.reply_to,b.words FROM messages m LEFT JOIN side_chat_brought b ON b.message_id=m.id "
            "WHERE m.organization_id=? AND m.author_id=? AND m.client_id=?",
            (org, user, client_id),
        ).fetchone()
        if existing is None:
            return None
        carried = {row[0] for row in db.execute("SELECT side_message_id FROM side_chat_brought_messages WHERE message_id=?",
                                                (existing["id"],))}
        if (existing["words"] is None or existing["topic_id"] != topic or existing["reply_to"] != reply_to
                or existing["words"] != words or carried != set(message_ids)):
            raise StoreError(409, "client_id was already used for a different message")
        return self._posted(db, existing["id"])

    def _posted(self, db, message_id: str) -> dict:
        row = db.execute(
            "SELECT m.*,u.name AS sender_name FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?", (message_id,)
        ).fetchone()
        return self.store._message(row, db)

    @staticmethod
    def _read_before(db, rows) -> int:
        """The side sequence the chosen replies read up to: each read the side chat before its own question."""
        questions = [row["reply_to"] for row in rows if row["sender_kind"] == "assistant" and row["reply_to"]]
        found: dict[str, int] = {}
        if questions:
            placeholders = ",".join("?" * len(questions))
            found = {row[0]: row[1] for row in db.execute(
                f"SELECT id,sequence FROM side_chat_messages WHERE id IN ({placeholders})", tuple(questions))}
        return max(found.get(row["reply_to"], row["sequence"]) for row in rows if row["sender_kind"] == "assistant")

    def _brought_text(self, org: str, topic: str, rows, owner: str, words: str, linked: list[str], omitted: int) -> str:
        """The post's text, which search, exports and later replies read: your words, what it builds on, the messages."""
        parts = [words] if words else []
        parts.append(f"From my side chat ({len(rows)} message{'s' if len(rows) != 1 else ''}):")
        context = []
        if len(linked) == 1:
            context.append(f"[my earlier post]({message_href(org, topic, linked[0])})")
        elif linked:
            links = [f"[{index}]({message_href(org, topic, identity)})" for index, identity in enumerate(linked, 1)]
            context.append("my earlier posts " + ", ".join(links[:-1]) + " and " + links[-1])
        if omitted:
            context.append(f"{omitted} side message{' that stays' if omitted == 1 else 's that stay'} private")
        if context:
            parts.append("_Builds on " + " and ".join(context) + "._")
        parts += [f"**{self._speaker(row, owner)}:** {row['content']}" for row in rows]
        return "\n\n".join(parts)


def brought_group(db, message: dict) -> dict | None:
    """The folded side-chat group a topic message brought: your words, its side messages, and what it builds on."""
    brought = db.execute(
        "SELECT owner_id,words,linked_message_ids_json,omitted_count FROM side_chat_brought WHERE message_id=?", (message["id"],)
    ).fetchone()
    if brought is None:
        return None
    rows = db.execute(
        "SELECT s.* FROM side_chat_brought_messages b JOIN side_chat_messages s ON s.id=b.side_message_id "
        "WHERE b.message_id=? ORDER BY s.sequence",
        (message["id"],),
    ).fetchall()
    linked = json.loads(brought["linked_message_ids_json"])
    posts: dict[str, int] = {}
    if linked:
        placeholders = ",".join("?" * len(linked))
        posts = {row[0]: row[1] for row in db.execute(
            f"SELECT id,created_at FROM messages WHERE organization_id=? AND topic_id=? AND id IN ({placeholders})",
            (message["organization_id"], message["topic_id"], *linked),
        )}
    return {
        "owner_id": brought["owner_id"],
        "words": brought["words"],
        "messages": [
            {"id": row["id"], "author_kind": row["sender_kind"], "assistant_model": row["assistant_model"],
             "content": row["content"], "created_at": row["created_at"]}
            for row in rows
        ],
        "linked": [{"message_id": identity, "created_at": posts[identity]} for identity in linked if identity in posts],
        "omitted": brought["omitted_count"],
    }


class SideMessage(RequestInput):
    content: Annotated[str, Field(min_length=1, max_length=16000, pattern=r"\S")]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]
    about_message_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    # A side reply at work that the message joins instead of asking a question of its own.
    adds_to: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None


class SideReply(RequestInput):
    question_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    models: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(min_length=1, max_length=1)]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]
    max_completion_tokens: Annotated[int | None, Field(ge=16, le=2**31 - 1, strict=True)] = None
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None


class SideBring(RequestInput):
    message_ids: Annotated[list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]], Field(min_length=1, max_length=BRING_LIMIT)]
    words: Annotated[str, Field(max_length=WORDS_LIMIT)] = ""
    reply_to: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    client_id: Annotated[str, Field(min_length=1, max_length=128)]


def install_routes(app, inference, provider, config, authenticated) -> None:
    User = Annotated[str, Depends(authenticated)]
    side = SideChats(inference)
    prefix = "/api/organizations/{organization_id}/topics/{topic_id}/side-chat"

    @app.get(prefix)
    def side_chat(organization_id: str, topic_id: str, user: User, after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0):
        return side.view(user, organization_id, topic_id, after)

    @app.post(prefix + "/messages", status_code=201)
    def side_message(organization_id: str, topic_id: str, body: SideMessage, user: User):
        return side.post(user, organization_id, topic_id, body.content, body.client_id, about=body.about_message_id,
                         adds_to=body.adds_to)

    @app.delete(prefix + "/additions/{message_id}")
    def side_addition_removed(organization_id: str, topic_id: str, message_id: str, user: User):
        return side.remove_addition(user, organization_id, topic_id, message_id)

    @app.post(prefix + "/replies", status_code=202)
    async def side_reply(organization_id: str, topic_id: str, body: SideReply, user: User):
        parameters = inference.parameters(body.models, body.max_completion_tokens, 50, None, effort=body.effort)
        existing = await run_in_threadpool(side.admit, user, organization_id, topic_id, body.client_id, parameters, body.question_id)
        if existing is not None:
            return existing
        if not config.inference_enabled:
            raise StoreError(503, "inference is disabled on this installation")
        quotes = await provider.quotes(body.models)
        inference.store._model_catalog = list(provider.catalog_models)
        return await run_in_threadpool(side.enqueue, user, organization_id, topic_id, body.client_id, parameters, quotes,
                                       body.question_id)

    @app.post(prefix + "/bring", status_code=201)
    def bring(organization_id: str, topic_id: str, body: SideBring, user: User):
        return side.bring(user, organization_id, topic_id, body.message_ids, body.words, body.reply_to, body.client_id)
