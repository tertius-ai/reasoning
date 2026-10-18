"""Access-checked resolution of message links and topic/project references.

Messages store a reference as `#[topic:ID]` or `#[project:ID]`, never the target's
name. A reader learns a name only through `resolve`, which applies the same
per-user topic and project checks as the hierarchy, so an inaccessible, deleted, or
absent target is indistinguishable (`None`).
"""

import re
from collections.abc import Iterable
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, Query

from .store import Store, StoreError

REFERENCE = re.compile(r"#\[(topic|project):([a-f0-9]{32})\]")
IDENTIFIER = re.compile(r"[a-f0-9]{32}")
# One request resolves at most this many distinct targets.
REFERENCE_LIMIT = 100
TOPIC_SYMBOL = {"alpha": "α", "beta": "β", "gamma": "γ"}


def _hidden(exc: StoreError) -> bool:
    return exc.status in (403, 404)


def _topic(store: Store, db, user: str, org: str, topic_id: str):
    try:
        _, project, _, channel, topic = store._topic_access(db, user, org, topic_id)
    except StoreError as exc:
        if _hidden(exc):
            return None
        raise
    return {"id": topic["id"], "name": topic["name"], "kind": topic["kind"], "channel_id": channel["id"],
            "channel_name": channel["name"], "project_id": project["id"], "project_name": project["name"]}


def _project(store: Store, db, user: str, org: str, project_id: str):
    try:
        _, project, _ = store._project_access(db, user, org, project_id)
    except StoreError as exc:
        if _hidden(exc):
            return None
        raise
    return {"id": project["id"], "name": project["name"]}


def resolve(store: Store, user: str, org: str, *, topics: Iterable[str] = (), projects: Iterable[str] = ()) -> dict:
    """Current names of the referenced targets this member can open; `None` for every other target."""
    topics, projects = list(dict.fromkeys(topics)), list(dict.fromkeys(projects))
    if len(topics) + len(projects) > REFERENCE_LIMIT:
        raise StoreError(400, f"resolve at most {REFERENCE_LIMIT} references at once")
    if not all(IDENTIFIER.fullmatch(value) for value in topics + projects):
        raise StoreError(400, "references are 32-character lowercase hexadecimal identifiers")
    with store.transaction() as db:
        store._require(db, user, org)
        return {"topics": {value: _topic(store, db, user, org, value) for value in topics},
                "projects": {value: _project(store, db, user, org, value) for value in projects}}


def message_window(store: Store, user: str, org: str, topic_id: str, message_id: str, limit: int = 100) -> dict:
    """A chronological page of at most `limit` messages around one message of a readable topic.

    Up to half the page precedes the message (fewer when the topic has fewer later messages);
    `older` reports whether earlier messages exist before the page.
    """
    if not 2 <= limit <= 200:
        raise StoreError(400, "invalid pagination")
    with store.transaction() as db:
        store._topic_access(db, user, org, topic_id)
        target = db.execute("SELECT sequence FROM messages WHERE organization_id=? AND topic_id=? AND id=?",
                            (org, topic_id, message_id)).fetchone()
        if target is None:
            raise StoreError(404, "message not found")
        query = ("SELECT m.*,u.name AS sender_name FROM messages m JOIN users u ON u.id=m.author_id "
                 "WHERE m.organization_id=? AND m.topic_id=? AND m.sequence")
        earlier = list(db.execute(query + "<=? ORDER BY m.sequence DESC LIMIT ?", (org, topic_id, target[0], limit + 1)))
        later = list(db.execute(query + ">? ORDER BY m.sequence LIMIT ?", (org, topic_id, target[0], limit)))
        before = min(len(earlier), max(limit // 2, limit - len(later)))
        after = min(len(later), limit - before)
        rows = list(reversed(earlier[:before])) + later[:after]
        return {"message_id": message_id, "messages": [store._message(row, db) for row in rows],
                "older": len(earlier) > before}


def topic_label(target: dict) -> str:
    return TOPIC_SYMBOL.get(target["kind"], "") + "-" + target["name"]


def reference_href(org: str, kind: str, target_id: str) -> str:
    return "/organization.html?" + urlencode({"organization": org, kind: target_id})


def link_references(store: Store, user: str, org: str, tokens) -> None:
    """Replace reference tokens in parsed Markdown-It text with links the reader may open.

    Each reader sees current names only for targets they can access; every other
    reference renders as the neutral text `unavailable`.
    """
    from markdown_it.token import Token

    inline = [token for token in tokens if token.type == "inline" and token.children]
    wanted = {"topic": [], "project": []}
    for token in inline:
        for child in token.children:
            if child.type == "text":
                for match in REFERENCE.finditer(child.content):
                    wanted[match[1]].append(match[2])
    if not any(wanted.values()):
        return
    topics, projects = list(dict.fromkeys(wanted["topic"])), list(dict.fromkeys(wanted["project"]))
    # Beyond the bound, extra references stay unavailable rather than failing the page.
    topics = topics[:REFERENCE_LIMIT]
    projects = projects[:REFERENCE_LIMIT - len(topics)]
    resolved = resolve(store, user, org, topics=topics, projects=projects)
    for token in inline:
        children = []
        for child in token.children:
            if child.type != "text" or not REFERENCE.search(child.content):
                children.append(child)
                continue
            position = 0
            for match in REFERENCE.finditer(child.content):
                if match.start() > position:
                    children.append(Token("text", "", 0, content=child.content[position:match.start()]))
                kind, target_id = match[1], match[2]
                target = resolved["topics" if kind == "topic" else "projects"].get(target_id)
                if target is None:
                    placeholder = Token("reference_open", "span", 1)
                    placeholder.attrs = {"class": "blog-reference blog-reference-unavailable"}
                    children += [placeholder, Token("text", "", 0, content="unavailable"), Token("reference_close", "span", -1)]
                else:
                    link = Token("link_open", "a", 1)
                    link.attrs = {"href": reference_href(org, kind, target_id), "class": "blog-reference"}
                    children += [link, Token("text", "", 0, content=topic_label(target) if kind == "topic" else target["name"]),
                                 Token("link_close", "a", -1)]
                position = match.end()
            if position < len(child.content):
                children.append(Token("text", "", 0, content=child.content[position:]))
        token.children = children


def install_routes(app, store: Store, authenticated) -> None:
    User = Annotated[str, Depends(authenticated)]
    # Comma-separated identifiers: 100 identifiers of 32 characters and their separators.
    Identifiers = Annotated[str, Query(max_length=REFERENCE_LIMIT * 33)]

    def split(value: str) -> list[str]:
        return [item for item in value.split(",") if item]

    @app.get("/api/organizations/{organization_id}/references")
    def references(organization_id: str, user: User, topics: Identifiers = "", projects: Identifiers = ""):
        return resolve(store, user, organization_id, topics=split(topics), projects=split(projects))

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/messages/{message_id}/context")
    def message_context(organization_id: str, topic_id: str, message_id: str, user: User,
                        limit: Annotated[int, Query(ge=2, le=200)] = 100):
        return message_window(store, user, organization_id, topic_id, message_id, limit)

