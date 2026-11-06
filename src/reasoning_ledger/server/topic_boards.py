"""Topic boards: a question in the middle, idea cards around it, and the routes of cards that lead to it.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

A board belongs to an AI chat (beta) topic and is shared with everyone who can read the topic; writers add,
edit and remove cards and links. Any card can open into a board of its own, whose question is the card, so a
board nests inside its parent card. A link runs from one card to the next on a route, or to the board's
question; an evidence link supports the question without being a step toward it.

The model arranges a board only when someone asks. An arrange run reads the topic's context and the board, and
calls ``board_arrange`` once: it sets each card's rank, maturity (idea, exploring or settled), theme, promise and
effort (0 to 1) and a one-line reason, may cite ledger statements, adds gap cards for missing steps, and lays the
routes into the question. Its own earlier gaps and links are replaced; people's cards and links stay. The run's
text is never posted to the topic, and topic run listings leave it out, so it does not appear as a chat reply.
"""

import json
import time
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from .request_input import RequestInput
from .store import StoreError, _id, _row

# Tools an arrange run may use: reading the topic and the ledger, and the one tool that applies the arrangement.
BOARD_TOOLS = frozenset({
    "board_arrange", "topic_history", "topic_message_read", "topic_search", "ledger_status", "ledger_search", "ledger_read",
    "knowledge_search", "knowledge_read",
})
MATURITY = ("idea", "exploring", "settled")
CARD_LIMIT = 200
TEXT_LIMIT = 1000
INSTRUCTION = (
    "Arrange the board above: call board_arrange exactly once, then reply with one short sentence. Routes are chains "
    "of cards that each lead to the next and finally to the board's question; add a gap card where a route is missing "
    "a step, and list cards that only give evidence separately. Rank the cards from most to least promising (1 first). "
    "Maturity is settled when the ledger establishes the card, exploring when work is under way, and idea otherwise. "
    "Give each card a short theme (two to four words), promise and effort between 0 and 1, and a reason of one sentence. "
    "Cite ledger statements (clm_ IDs) only when you have read them. Do not invent cards for ideas that are not on the board."
)


def listed_request(column: str) -> str:
    """SQL that holds when the model request in ``column`` is not a board arrangement, which chat listings leave out."""
    return f"NOT EXISTS (SELECT 1 FROM board_runs br WHERE br.request_id={column})"


class TopicBoards:
    def __init__(self, inference):
        self.inference, self.store = inference, inference.store

    def _topic(self, db, user: str, org: str, topic: str, *, write: bool = False) -> dict:
        target = self.store._topic_access(db, user, org, topic, write=write)[4]
        if target["kind"] != "beta":
            raise StoreError(400, "boards belong to AI chat topics")
        return target

    def _board(self, db, user: str, org: str, board: str, *, write: bool = False) -> dict:
        row = db.execute("SELECT * FROM topic_boards WHERE organization_id=? AND id=?", (org, board)).fetchone()
        if row is None:
            raise StoreError(404, "board not found")
        self._topic(db, user, org, row["topic_id"], write=write)
        return dict(row)

    @staticmethod
    def _card(db, board: str, card: str) -> dict:
        row = db.execute("SELECT * FROM board_cards WHERE board_id=? AND id=? AND deleted_at IS NULL", (board, card)).fetchone()
        if row is None:
            raise StoreError(404, "card not found on this board")
        return dict(row)

    @staticmethod
    def _text(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > TEXT_LIMIT:
            raise StoreError(400, f"a card needs text of at most {TEXT_LIMIT} characters")
        return value.strip()

    @staticmethod
    def _touch(db, board: str) -> None:
        db.execute("UPDATE topic_boards SET updated_at=? WHERE id=?", (int(time.time()), board))

    @staticmethod
    def _card_view(db, row) -> dict:
        card = {key: row[key] for key in ("id", "kind", "text", "author_kind", "created_by", "created_at", "updated_at", "rank",
                                          "maturity", "theme", "promise", "effort", "reason")}
        card["statements"] = json.loads(row["statements_json"])
        child = db.execute("SELECT id FROM topic_boards WHERE parent_card_id=?", (row["id"],)).fetchone()
        card["board"] = None
        if child is not None:
            counts = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(CASE WHEN kind='gap' THEN 1 ELSE 0 END),0) FROM board_cards "
                "WHERE board_id=? AND deleted_at IS NULL", (child[0],)
            ).fetchone()
            card["board"] = {"id": child[0], "cards": counts[0], "gaps": counts[1]}
        return card

    def _content(self, db, board: dict) -> dict:
        cards = [self._card_view(db, row) for row in db.execute(
            "SELECT * FROM board_cards WHERE board_id=? AND deleted_at IS NULL ORDER BY created_at,id", (board["id"],))]
        links = [dict(row) for row in db.execute(
            "SELECT id,source_card_id,target_card_id,kind,author_kind FROM board_links WHERE board_id=? ORDER BY created_at,id",
            (board["id"],))]
        return {"id": board["id"], "question": board["question"], "parent_card_id": board["parent_card_id"],
                "arranged_at": board["arranged_at"], "cards": cards, "links": links}

    def _view(self, db, board: dict) -> dict:
        result = {**self._content(db, board), "topic_id": board["topic_id"], "created_by": board["created_by"],
                  "created_at": board["created_at"], "updated_at": board["updated_at"]}
        # The boards inside this board's cards, so they can open in place as the view zooms in.
        result["children"] = {}
        for card in result["cards"]:
            if card["board"]:
                child = dict(db.execute("SELECT * FROM topic_boards WHERE id=?", (card["board"]["id"],)).fetchone())
                result["children"][card["id"]] = self._content(db, child)
        # The boards above this one, outermost first, each with the card that holds the next.
        path, current = [], board
        while current["parent_card_id"] and len(path) < 20:
            card = db.execute("SELECT id,board_id,text FROM board_cards WHERE id=?", (current["parent_card_id"],)).fetchone()
            if card is None:
                break
            current = dict(db.execute("SELECT * FROM topic_boards WHERE id=?", (card["board_id"],)).fetchone())
            path.insert(0, {"board_id": current["id"], "question": current["question"], "card_id": card["id"], "card": card["text"]})
        result["path"] = path
        run = db.execute(
            "SELECT r.id,r.status,r.model,r.error_code,b.applied_at FROM board_runs b JOIN llm_runs r ON r.request_id=b.request_id "
            "JOIN llm_requests q ON q.id=b.request_id WHERE b.board_id=? ORDER BY q.created_at DESC,r.id DESC LIMIT 1",
            (board["id"],),
        ).fetchone()
        result["arranging"] = dict(run) if run else None
        return result

    def boards(self, user: str, org: str, topic: str) -> list[dict]:
        with self.store.transaction() as db:
            self._topic(db, user, org, topic)
            return [
                {**dict(row), "cards": row["cards"]}
                for row in db.execute(
                    "SELECT b.id,b.question,b.created_by,b.created_at,b.updated_at,b.arranged_at,"
                    "(SELECT COUNT(*) FROM board_cards c WHERE c.board_id=b.id AND c.deleted_at IS NULL) AS cards "
                    "FROM topic_boards b WHERE b.organization_id=? AND b.topic_id=? AND b.parent_card_id IS NULL "
                    "ORDER BY b.updated_at DESC,b.id LIMIT 100",
                    (org, topic),
                )
            ]

    def create(self, user: str, org: str, topic: str, question: str) -> dict:
        question = self._text(question)
        with self.store.transaction(write=True) as db:
            self._topic(db, user, org, topic, write=True)
            identity, now = _id(), int(time.time())
            db.execute(
                "INSERT INTO topic_boards(id,organization_id,topic_id,question,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (identity, org, topic, question, user, now, now),
            )
            self.store._audit(db, org, user, "board.create", identity)
            return self._view(db, _row(db.execute("SELECT * FROM topic_boards WHERE id=?", (identity,)).fetchone()))

    def view(self, user: str, org: str, board: str) -> dict:
        with self.store.transaction() as db:
            return self._view(db, self._board(db, user, org, board))

    def add_card(self, user: str, org: str, board: str, text: str) -> dict:
        text = self._text(text)
        with self.store.transaction(write=True) as db:
            target = self._board(db, user, org, board, write=True)
            if db.execute("SELECT COUNT(*) FROM board_cards WHERE board_id=? AND deleted_at IS NULL", (board,)).fetchone()[0] >= CARD_LIMIT:
                raise StoreError(409, f"a board holds at most {CARD_LIMIT} cards")
            now = int(time.time())
            db.execute(
                "INSERT INTO board_cards(id,organization_id,board_id,kind,text,author_kind,created_by,created_at,updated_at) "
                "VALUES(?,?,?,'idea',?,'human',?,?,?)",
                (_id(), org, board, text, user, now, now),
            )
            self._touch(db, board)
            return self._view(db, target)

    def edit_card(self, user: str, org: str, board: str, card: str, text: str) -> dict:
        text = self._text(text)
        with self.store.transaction(write=True) as db:
            target = self._board(db, user, org, board, write=True)
            self._card(db, board, card)
            db.execute("UPDATE board_cards SET text=?,updated_at=? WHERE id=?", (text, int(time.time()), card))
            child = db.execute("SELECT id FROM topic_boards WHERE parent_card_id=?", (card,)).fetchone()
            if child is not None:
                db.execute("UPDATE topic_boards SET question=?,updated_at=? WHERE id=?", (text, int(time.time()), child[0]))
            self._touch(db, board)
            return self._view(db, target)

    def remove_card(self, user: str, org: str, board: str, card: str) -> dict:
        with self.store.transaction(write=True) as db:
            target = self._board(db, user, org, board, write=True)
            self._card(db, board, card)
            db.execute("UPDATE board_cards SET deleted_at=? WHERE id=?", (int(time.time()), card))
            db.execute("DELETE FROM board_links WHERE board_id=? AND (source_card_id=? OR target_card_id=?)", (board, card, card))
            self._touch(db, board)
            return self._view(db, target)

    def link(self, user: str, org: str, board: str, source: str, target: str | None, kind: str) -> dict:
        if kind not in {"route", "evidence"} or source == target:
            raise StoreError(400, "a link joins two different cards, or a card and the question, as a route or evidence")
        with self.store.transaction(write=True) as db:
            current = self._board(db, user, org, board, write=True)
            self._card(db, board, source)
            if target is not None:
                self._card(db, board, target)
            if not self._linked(db, board, source, target):
                db.execute(
                    "INSERT INTO board_links(id,organization_id,board_id,source_card_id,target_card_id,kind,created_by,author_kind,"
                    "created_at) VALUES(?,?,?,?,?,?,?,'human',?)",
                    (_id(), org, board, source, target, kind, user, int(time.time())),
                )
                self._touch(db, board)
            return self._view(db, current)

    @staticmethod
    def _linked(db, board: str, source: str, target: str | None) -> bool:
        if target is None:
            return db.execute("SELECT 1 FROM board_links WHERE board_id=? AND source_card_id=? AND target_card_id IS NULL",
                              (board, source)).fetchone() is not None
        return db.execute("SELECT 1 FROM board_links WHERE board_id=? AND source_card_id=? AND target_card_id=?",
                          (board, source, target)).fetchone() is not None

    def unlink(self, user: str, org: str, board: str, link: str) -> dict:
        with self.store.transaction(write=True) as db:
            current = self._board(db, user, org, board, write=True)
            if db.execute("DELETE FROM board_links WHERE board_id=? AND id=?", (board, link)).rowcount == 0:
                raise StoreError(404, "link not found on this board")
            self._touch(db, board)
            return self._view(db, current)

    def open_card(self, user: str, org: str, board: str, card: str) -> dict:
        """The board inside a card, created on first use with the card's text as its question."""
        with self.store.transaction(write=True) as db:
            parent = self._board(db, user, org, board, write=True)
            row = self._card(db, board, card)
            child = db.execute("SELECT * FROM topic_boards WHERE parent_card_id=?", (card,)).fetchone()
            if child is None:
                identity, now = _id(), int(time.time())
                db.execute(
                    "INSERT INTO topic_boards(id,organization_id,topic_id,parent_card_id,question,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (identity, org, parent["topic_id"], card, row["text"], user, now, now),
                )
                child = db.execute("SELECT * FROM topic_boards WHERE id=?", (identity,)).fetchone()
            return self._view(db, dict(child))

    # Arrangement --------------------------------------------------------------------------------------------------

    def _active(self, db, board: str):
        return db.execute(
            "SELECT r.id FROM board_runs b JOIN llm_runs r ON r.request_id=b.request_id WHERE b.board_id=? "
            "AND r.status IN ('queued','running')", (board,)
        ).fetchone()

    def _replayed(self, db, user: str, org: str, board: str, client: str, parameters: dict):
        request = db.execute(
            "SELECT q.*,b.board_id FROM llm_requests q LEFT JOIN board_runs b ON b.request_id=q.id "
            "WHERE q.organization_id=? AND q.requested_by=? AND q.client_id=?", (org, user, client)
        ).fetchone()
        if request is None:
            return None
        if request["board_id"] != board or json.loads(request["parameters_json"]) != parameters:
            raise StoreError(409, "client_id was already used for a different model request")
        return self.inference._request_runs(db, request["id"])

    def admit(self, user: str, org: str, board: str, client: str, parameters: dict) -> list[dict] | None:
        with self.store.transaction() as db:
            target = self._board(db, user, org, board, write=True)
            existing = self._replayed(db, user, org, board, client, parameters)
            if existing is None:
                if self._active(db, board):
                    raise StoreError(409, "this board is already being arranged")
                self.inference._preflight(db, user, org, target["topic_id"])
            return existing

    def _record(self, db, board: dict) -> dict:
        content = self._content(db, board)
        record = {
            "question": content["question"],
            "cards": [{key: card[key] for key in ("id", "kind", "text", "author_kind", "rank", "maturity", "theme", "promise",
                                                    "effort", "reason", "statements")} for card in content["cards"]],
            "links": [{"from": link["source_card_id"], "to": link["target_card_id"] or "question", "kind": link["kind"],
                       "by": link["author_kind"]} for link in content["links"]],
        }
        if board["parent_card_id"]:
            parent = db.execute(
                "SELECT c.text,b.question FROM board_cards c JOIN topic_boards b ON b.id=c.board_id WHERE c.id=?",
                (board["parent_card_id"],),
            ).fetchone()
            if parent is not None:
                record["inside_card"] = {"card": parent["text"], "of_board_question": parent["question"]}
        return record

    def enqueue(self, user: str, org: str, board: str, client: str, parameters: dict, quotes) -> list[dict]:
        from .research_context import ResearchContext, encoded, token_bound

        with self.store.transaction(write=True) as db:
            target = self._board(db, user, org, board, write=True)
            existing = self._replayed(db, user, org, board, client, parameters)
            if existing is not None:
                return existing
            if self._active(db, board):
                raise StoreError(409, "this board is already being arranged")
            topic = target["topic_id"]
            through = db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM messages WHERE organization_id=? AND topic_id=?", (org, topic)
            ).fetchone()[0]
            messages, snapshot = ResearchContext(self.store).compile(db, org, topic, through, quotes, user=user)
            compiled = [*messages[:-1], {"role": "system", "content": "The board to arrange (reference data): "
                                         + encoded(self._record(db, target))}, {"role": "user", "content": INSTRUCTION}]
            snapshot = {**snapshot, "messages": compiled, "board_arrange": board, "through_sequence": 0,
                        "memory_through_sequence": 0, "compact_through_sequence": 0, "input_token_upper_bound": token_bound(compiled)}
            runs = self.inference._enqueue(db, user, org, topic, client, parameters, quotes, research_override=(compiled, snapshot))
            db.execute("INSERT INTO board_runs(request_id,organization_id,board_id) VALUES(?,?,?)", (runs[0]["request_id"], org, board))
            self.store._audit(db, org, user, "board.arrange", board)
            return self.inference._request_runs(db, runs[0]["request_id"])

    def apply(self, run: dict, board: str, arrangement: dict) -> dict:
        """Apply one arrangement from an arrange run: attributes, gap cards, and the model's routes and evidence."""
        cards, gaps = arrangement.get("cards") or [], arrangement.get("gaps") or []
        routes, evidence = arrangement.get("routes") or [], arrangement.get("evidence") or []
        with self.store.transaction(write=True) as db:
            marker = db.execute("SELECT applied_at FROM board_runs WHERE request_id=? AND board_id=?",
                                (run["request_id"], board)).fetchone()
            if marker is None:
                raise StoreError(403, "this run does not arrange this board")
            if marker["applied_at"] is not None:
                raise StoreError(409, "this run already arranged the board")
            target = self._board(db, run["requested_by"], run["organization_id"], board, write=True)
            known = {row["id"] for row in db.execute("SELECT id FROM board_cards WHERE board_id=? AND deleted_at IS NULL", (board,))}
            now = int(time.time())
            org, user = run["organization_id"], run["requested_by"]

            def attributes(item: dict) -> tuple:
                maturity = item.get("maturity")
                statements = [value for value in item.get("statements") or [] if isinstance(value, str)][:6]
                if statements and any(not db.execute(
                    "SELECT 1 FROM mathematical_records WHERE organization_id=? AND object_id=? AND record_type='claim-head'",
                    (org, statement)).fetchone() for statement in statements):
                    raise StoreError(400, "cite only ledger statements that exist (clm_ IDs)")
                rank = item.get("rank")
                return (rank if isinstance(rank, int) and rank >= 1 else None, maturity if maturity in MATURITY else None,
                        (item.get("theme") or None) and str(item["theme"])[:60],
                        self._unit(item.get("promise")), self._unit(item.get("effort")),
                        (item.get("reason") or None) and str(item["reason"])[:300], json.dumps(statements))

            for item in cards:
                if item.get("card_id") not in known:
                    raise StoreError(400, "arrange only cards that are on this board")
                db.execute("UPDATE board_cards SET rank=?,maturity=?,theme=?,promise=?,effort=?,reason=?,statements_json=?,updated_at=? "
                           "WHERE id=?", (*attributes(item), now, item["card_id"]))
            # The model's earlier gaps are replaced, except one that someone opened into a board of its own.
            db.execute("UPDATE board_cards SET deleted_at=? WHERE board_id=? AND kind='gap' AND author_kind='assistant' "
                       "AND deleted_at IS NULL AND id NOT IN (SELECT parent_card_id FROM topic_boards WHERE parent_card_id IS NOT NULL)",
                       (now, board))
            db.execute("DELETE FROM board_links WHERE board_id=? AND author_kind='assistant'", (board,))
            known = {row["id"] for row in db.execute("SELECT id FROM board_cards WHERE board_id=? AND deleted_at IS NULL", (board,))}
            keys: dict[str, str] = {}
            for item in gaps[:30]:
                key, text = item.get("key"), item.get("text")
                if not isinstance(key, str) or not key or key in keys or key in known:
                    raise StoreError(400, "each gap needs its own key")
                identity = _id()
                db.execute(
                    "INSERT INTO board_cards(id,organization_id,board_id,kind,text,author_kind,created_by,created_at,updated_at,"
                    "rank,maturity,theme,promise,effort,reason,statements_json) VALUES(?,?,?,'gap',?,'assistant',?,?,?,?,?,?,?,?,?,?)",
                    (identity, org, board, self._text(text), user, now, now, *attributes(item)),
                )
                keys[key] = identity

            def resolve(reference) -> str:
                identity = keys.get(reference, reference)
                if identity not in known and identity not in keys.values():
                    raise StoreError(400, "a route names a card or gap that is not on this board")
                return identity

            pairs: set[tuple[str, str | None, str]] = set()
            for route in routes[:20]:
                steps = [resolve(reference) for reference in (route or [])[:12]]
                for source, follower in zip(steps, [*steps[1:], None], strict=True):
                    if source != follower:
                        pairs.add((source, follower, "route"))
            for reference in evidence[:30]:
                pairs.add((resolve(reference), None, "evidence"))
            for source, follower, kind in sorted(pairs, key=lambda pair: (pair[0], pair[1] or "", pair[2])):
                if self._linked(db, board, source, follower):
                    continue
                db.execute(
                    "INSERT INTO board_links(id,organization_id,board_id,source_card_id,target_card_id,kind,created_by,author_kind,"
                    "created_at) VALUES(?,?,?,?,?,?,?,'assistant',?)",
                    (_id(), org, board, source, follower, kind, user, now),
                )
            db.execute("UPDATE board_runs SET applied_at=? WHERE request_id=?", (now, run["request_id"]))
            db.execute("UPDATE topic_boards SET arranged_at=?,updated_at=? WHERE id=?", (now, now, board))
            return {"applied": True, "cards": len(cards), "gaps": len(keys), "links": len(pairs), "board": target["id"]}

    @staticmethod
    def _unit(value) -> float | None:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1 else None


def card_context(db, org: str, topic: str, card: str) -> dict | None:
    """A card a side question is about: its text, model attributes, board question and the steps around it."""
    row = db.execute(
        "SELECT c.*,b.question,b.topic_id FROM board_cards c JOIN topic_boards b ON b.id=c.board_id "
        "WHERE c.organization_id=? AND c.id=? AND c.deleted_at IS NULL", (org, card)
    ).fetchone()
    if row is None or row["topic_id"] != topic:
        return None

    def steps(query: str) -> list[str]:
        return [item[0] for item in db.execute(query, (card,))]

    return {
        "card": row["text"], "kind": row["kind"], "board_question": row["question"],
        **{key: row[key] for key in ("rank", "maturity", "theme", "promise", "effort", "reason") if row[key] is not None},
        "leads_to": steps("SELECT COALESCE(c.text,'the question') FROM board_links l LEFT JOIN board_cards c ON c.id=l.target_card_id "
                          "WHERE l.source_card_id=?"),
        "comes_from": steps("SELECT c.text FROM board_links l JOIN board_cards c ON c.id=l.source_card_id WHERE l.target_card_id=?"),
    }


def register_arrange(research, board: str) -> None:
    """The arrange run's one write tool."""
    from .research_tools import schema, string

    attributes = {
        "rank": {"type": "integer", "minimum": 1, "maximum": 500},
        "maturity": {"enum": list(MATURITY)},
        "theme": string(60),
        "promise": {"type": "number", "minimum": 0, "maximum": 1},
        "effort": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": string(300),
        "statements": {"type": "array", "maxItems": 6, "items": string(64)},
    }
    boards = TopicBoards(research.inference)

    @research.registry.tool(
        name="board_arrange",
        description="Apply your arrangement of the board once: card attributes, gap cards for missing steps, routes of card IDs "
        "or gap keys that end at the question, and evidence cards. Replaces your earlier gaps and links; people's stay.",
        risk="write",
        input_schema=schema({
            "cards": {"type": "array", "maxItems": 200, "items": schema({"card_id": string(64), **attributes}, ["card_id"])},
            "gaps": {"type": "array", "maxItems": 30, "items": schema({"key": string(32), "text": string(TEXT_LIMIT), **attributes},
                                                                      ["key", "text"])},
            "routes": {"type": "array", "maxItems": 20, "items": {"type": "array", "minItems": 1, "maxItems": 12, "items": string(64)}},
            "evidence": {"type": "array", "maxItems": 30, "items": string(64)},
        }, ["cards", "routes"]),
    )
    def arrange(cards, routes, gaps=None, evidence=None):
        return boards.apply(research.row, board, {"cards": cards, "routes": routes, "gaps": gaps or [], "evidence": evidence or []})


class Empty(RequestInput):
    pass


class BoardQuestion(RequestInput):
    question: Annotated[str, Field(min_length=1, max_length=TEXT_LIMIT, pattern=r"\S")]


class CardText(RequestInput):
    text: Annotated[str, Field(min_length=1, max_length=TEXT_LIMIT, pattern=r"\S")]


class Link(RequestInput):
    source_card_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    target_card_id: Annotated[str | None, Field(pattern=r"^[a-f0-9]{32}$")] = None
    kind: Literal["route", "evidence"] = "route"


class Arrange(RequestInput):
    models: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(min_length=1, max_length=1)]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]
    max_completion_tokens: Annotated[int | None, Field(ge=16, le=2**31 - 1, strict=True)] = None
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None


def install_routes(app, inference, provider, config, authenticated) -> None:
    User = Annotated[str, Depends(authenticated)]
    boards = TopicBoards(inference)
    topic = "/api/organizations/{organization_id}/topics/{topic_id}/boards"
    board = "/api/organizations/{organization_id}/boards/{board_id}"

    @app.get(topic)
    def topic_boards(organization_id: str, topic_id: str, user: User):
        return boards.boards(user, organization_id, topic_id)

    @app.post(topic, status_code=201)
    def create_board(organization_id: str, topic_id: str, body: BoardQuestion, user: User):
        return boards.create(user, organization_id, topic_id, body.question)

    @app.get(board)
    def read_board(organization_id: str, board_id: str, user: User):
        return boards.view(user, organization_id, board_id)

    @app.post(board + "/cards", status_code=201)
    def add_card(organization_id: str, board_id: str, body: CardText, user: User):
        return boards.add_card(user, organization_id, board_id, body.text)

    @app.patch(board + "/cards/{card_id}")
    def edit_card(organization_id: str, board_id: str, card_id: str, body: CardText, user: User):
        return boards.edit_card(user, organization_id, board_id, card_id, body.text)

    @app.delete(board + "/cards/{card_id}")
    def remove_card(organization_id: str, board_id: str, card_id: str, user: User):
        return boards.remove_card(user, organization_id, board_id, card_id)

    @app.post(board + "/cards/{card_id}/board")
    def open_card(organization_id: str, board_id: str, card_id: str, body: Empty, user: User):
        return boards.open_card(user, organization_id, board_id, card_id)

    @app.post(board + "/links", status_code=201)
    def link(organization_id: str, board_id: str, body: Link, user: User):
        return boards.link(user, organization_id, board_id, body.source_card_id, body.target_card_id, body.kind)

    @app.delete(board + "/links/{link_id}")
    def unlink(organization_id: str, board_id: str, link_id: str, user: User):
        return boards.unlink(user, organization_id, board_id, link_id)

    @app.post(board + "/arrange", status_code=202)
    async def arrange(organization_id: str, board_id: str, body: Arrange, user: User):
        parameters = inference.parameters(body.models, body.max_completion_tokens, 50, None, effort=body.effort)
        existing = await run_in_threadpool(boards.admit, user, organization_id, board_id, body.client_id, parameters)
        if existing is not None:
            return existing
        if not config.inference_enabled:
            raise StoreError(503, "inference is disabled on this installation")
        quotes = await provider.quotes(body.models)
        inference.store._model_catalog = list(provider.catalog_models)
        return await run_in_threadpool(boards.enqueue, user, organization_id, board_id, body.client_id, parameters, quotes)
