"""Private side chats: one member's own conversation with a model, beside a topic and reading its context.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

A side chat belongs to one member and one AI chat (beta) topic. Its messages live in ``side_chat_messages``,
outside the topic, so no topic, search, webhook, export or knowledge path reads them. Every read and write here
is the caller's own side chat; nobody else, organization administrators included, can open it.

A reply is an ordinary model run of the topic, so it reads the topic's shared context, and the ledger statements
it records belong to the topic as usual. ``side_chat_runs`` marks the run private: run listings, live updates,
research views and exports leave it out, only its requester can open or cancel it, and its answer is stored here
instead of being posted to the topic. Its tools are limited to reading and to ledger writes (``SIDE_CHAT_TOOLS``).

Bringing replies to the topic posts one message from the owner with the chosen side messages in their original
order; the conversation shows it as a folded group. With a context note, only the chosen exchange is posted, and
a line says how many earlier side messages it built on and which ledger statements those recorded.
"""

import json
import time
from typing import Annotated, Literal

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
# A brought group longer than this is refused; choose fewer messages or a context note.
BRING_CHARACTERS = 64_000
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


def run_statements(db, org: str, user: str, run_id: str | None) -> list[dict]:
    """Ledger statements a run recorded or revised, oldest first, each with its latest statement text."""
    if not run_id:
        return []
    statements: dict[str, dict] = {}
    for (result,) in db.execute(
        "SELECT result_json FROM mathematical_requests WHERE organization_id=? AND actor_id=? AND client_id LIKE ?",
        (org, user, run_id + ":%"),
    ):
        value = json.loads(result)
        if isinstance(value, dict) and isinstance(value.get("claim_id"), str):
            statement = statements.setdefault(value["claim_id"], {"claim_id": value["claim_id"]})
            if isinstance(value.get("statement_text"), str):
                statement["statement_text"] = value["statement_text"]
    return list(statements.values())


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
        result = _row(row)
        result["author_kind"] = result.pop("sender_kind")
        card = result.pop("card_id", None)
        if card:
            # A question asked about a board card names the card, as it reads now.
            found = db.execute("SELECT id,board_id,text FROM board_cards WHERE id=?", (card,)).fetchone()
            result["card"] = dict(found) if found else {"id": card, "board_id": None, "text": None}
        if result["author_kind"] == "assistant":
            result["statements"] = run_statements(db, result["organization_id"], result["owner_id"], result["inference_run_id"])
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

    def post(self, user: str, org: str, topic: str, content: str, client_id: str, card: str | None = None) -> dict:
        """Add a question to the caller's side chat, optionally about a card on one of the topic's boards."""
        from .topic_boards import card_context

        if not isinstance(content, str) or not content.strip() or len(content) > 16000 or not client_id or len(client_id) > 128:
            raise StoreError(400, "a side message needs text of at most 16000 characters and a client_id")
        with self.store.transaction(write=True) as db:
            self._access(db, user, org, topic, write=True)
            existing = db.execute(
                "SELECT * FROM side_chat_messages WHERE organization_id=? AND owner_id=? AND client_id=?", (org, user, client_id)
            ).fetchone()
            if existing:
                if (existing["topic_id"] != topic or existing["content"] != content or existing["sender_kind"] != "human"
                        or existing["card_id"] != card):
                    raise StoreError(409, "client_id was already used for a different side message")
                return self._message(db, existing)
            if card is not None and card_context(db, org, topic, card) is None:
                raise StoreError(404, "card not found on this topic's boards")
            message_id = _id()
            db.execute(
                "INSERT INTO side_chat_messages(id,organization_id,topic_id,owner_id,sender_kind,content,client_id,created_at,card_id) "
                "VALUES(?,?,?,?,'human',?,?,?,?)",
                (message_id, org, topic, user, content, client_id, int(time.time()), card),
            )
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
        about = []
        if question["card_id"]:
            from .topic_boards import card_context

            card = card_context(db, org, topic, question["card_id"])
            if card is not None:
                about = [{"role": "system", "content": "The latest side question is about this card on the topic's board "
                          "(reference data): " + encoded(card)}]
        compiled = [
            *shared,
            {"role": "system", "content": "The topic's shared conversation ends here. What follows is the private side chat of "
             + encoded(owner) + " about this topic; only they see it."},
            *side,
            *about,
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

    def bring(self, user: str, org: str, topic: str, message_ids: list[str], note: bool, client_id: str) -> dict:
        """Post the chosen side messages to the topic as one message from the caller, shown as a folded group."""
        if not 1 <= len(message_ids) <= BRING_LIMIT or len(set(message_ids)) != len(message_ids):
            raise StoreError(400, f"choose between one and {BRING_LIMIT} distinct side messages")
        with self.store.transaction(write=True) as db:
            self._access(db, user, org, topic, write=True)
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
            owner = db.execute("SELECT name FROM users WHERE id=?", (user,)).fetchone()[0]
            omitted = []
            if note:
                omitted = db.execute(
                    "SELECT * FROM side_chat_messages WHERE organization_id=? AND topic_id=? AND owner_id=? AND sequence<? "
                    "ORDER BY sequence",
                    (org, topic, user, rows[0]["sequence"]),
                ).fetchall()
            parts = [f"From my side chat ({len(rows)} message{'s' if len(rows) != 1 else ''}):"]
            if omitted:
                cited = ", ".join(
                    statement["claim_id"] for row in omitted if row["sender_kind"] == "assistant"
                    for statement in run_statements(db, org, user, row["inference_run_id"])
                )
                parts.append(
                    f"_Context: this builds on {len(omitted)} earlier side message{'s' if len(omitted) != 1 else ''} that stay private"
                    + (f"; they recorded {cited}._" if cited else "._")
                )
            parts += [f"**{self._speaker(row, owner)}:** {row['content']}" for row in rows]
            content = "\n\n".join(parts)
            if len(content) > BRING_CHARACTERS:
                raise StoreError(413, "these side messages are too long to bring at once; choose fewer, or bring one exchange with a note")
            message = self.store._post_message(db, user, org, topic, content, client_id)
            db.execute(
                "INSERT INTO side_chat_brought(message_id,organization_id,topic_id,owner_id,side_message_ids_json,omitted_count,"
                "created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(message_id) DO NOTHING",
                (message["id"], org, topic, user, json.dumps([row["id"] for row in rows]), len(omitted), int(time.time())),
            )
            self.store._audit(db, org, user, "side_chat.bring", message["id"])
            row = db.execute(
                "SELECT m.*,u.name AS sender_name FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?", (message["id"],)
            ).fetchone()
            return self.store._message(row, db)


def brought_group(db, message: dict) -> dict | None:
    """The folded side-chat group a topic message brought, read from the side messages it names."""
    brought = db.execute(
        "SELECT owner_id,side_message_ids_json,omitted_count FROM side_chat_brought WHERE message_id=?", (message["id"],)
    ).fetchone()
    if brought is None:
        return None
    identities = json.loads(brought["side_message_ids_json"])
    placeholders = ",".join("?" * len(identities))
    rows = db.execute(
        f"SELECT * FROM side_chat_messages WHERE organization_id=? AND owner_id=? AND id IN ({placeholders}) ORDER BY sequence",
        (message["organization_id"], brought["owner_id"], *identities),
    ).fetchall()
    org, owner = message["organization_id"], brought["owner_id"]
    group = {
        "owner_id": owner,
        "messages": [
            {"id": row["id"], "author_kind": row["sender_kind"], "assistant_model": row["assistant_model"],
             "content": row["content"], "created_at": row["created_at"],
             "statements": run_statements(db, org, owner, row["inference_run_id"]) if row["sender_kind"] == "assistant" else []}
            for row in rows
        ],
        "omitted": brought["omitted_count"],
    }
    if brought["omitted_count"]:
        first = rows[0]["sequence"] if rows else 0
        group["omitted_statements"] = [
            statement
            for row in db.execute(
                "SELECT inference_run_id FROM side_chat_messages WHERE organization_id=? AND topic_id=? AND owner_id=? "
                "AND sequence<? AND sender_kind='assistant' ORDER BY sequence",
                (org, message["topic_id"], owner, first),
            )
            for statement in run_statements(db, org, owner, row["inference_run_id"])
        ]
    return group


class SideMessage(RequestInput):
    content: Annotated[str, Field(min_length=1, max_length=16000, pattern=r"\S")]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]
    card_id: Annotated[str | None, Field(pattern=r"^[a-f0-9]{32}$")] = None


class SideReply(RequestInput):
    question_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    models: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(min_length=1, max_length=1)]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]
    max_completion_tokens: Annotated[int | None, Field(ge=16, le=2**31 - 1, strict=True)] = None
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None


class SideBring(RequestInput):
    message_ids: Annotated[list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]], Field(min_length=1, max_length=BRING_LIMIT)]
    note: bool = False
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
        return side.post(user, organization_id, topic_id, body.content, body.client_id, body.card_id)

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
        return side.bring(user, organization_id, topic_id, body.message_ids, body.note, body.client_id)
