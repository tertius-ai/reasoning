"""Public human-feedback and beta-fork routes over the canonical topic ACL."""

from typing import Annotated, Literal

from fastapi import Depends, Query
from pydantic import Field

from .chat_feedback import ChatFeedback
from .reaction_emoji_schema import EMOJIS
from .request_input import RequestInput
from .topic_forks import TopicForks


class Reaction(RequestInput):
    emoji: Literal[EMOJIS]
    active: Annotated[bool, Field(strict=True)]


class Fork(RequestInput):
    message_id: Annotated[str, Field(min_length=1, max_length=128)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]


def install_feedback_routes(app, store, authenticated):
    User = Annotated[str, Depends(authenticated)]
    feedback, forks = ChatFeedback(store), TopicForks(store)

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/reactions/messages")
    def message_reactions(
        organization_id: str,
        topic_id: str,
        user: User,
        ids: Annotated[str, Query(min_length=1, max_length=200 * 129)],
    ):
        """Reactions for a page of messages (comma-separated IDs), keyed by message ID."""
        return feedback.read_messages(user, organization_id, topic_id, [item for item in ids.split(",") if item])

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/reactions/{kind}/{target_id}")
    def reactions(
        organization_id: str,
        topic_id: str,
        kind: Literal["message", "run", "bot", "node"],
        target_id: str,
        user: User,
        node_id: Annotated[str | None, Query(max_length=200)] = None,
    ):
        return feedback.read(user, organization_id, topic_id, kind, target_id, node_id)

    @app.put("/api/organizations/{organization_id}/topics/{topic_id}/reactions/{kind}/{target_id}")
    def reaction(
        organization_id: str,
        topic_id: str,
        kind: Literal["message", "run", "bot", "node"],
        target_id: str,
        body: Reaction,
        user: User,
        node_id: Annotated[str | None, Query(max_length=200)] = None,
    ):
        return feedback.set(user, organization_id, topic_id, kind, target_id, body.emoji, body.active, node_id)

    @app.get("/api/organizations/{organization_id}/model-feedback")
    def model_feedback(organization_id: str, user: User, project_id: str | None = None):
        return feedback.totals(user, organization_id, project_id)

    @app.post("/api/organizations/{organization_id}/topics/{topic_id}/forks", status_code=201)
    def fork(organization_id: str, topic_id: str, body: Fork, user: User):
        return forks.create(user, organization_id, topic_id, body.message_id, body.name, body.client_id)

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/fork-origin")
    def fork_origin(organization_id: str, topic_id: str, user: User):
        return forks.origin(user, organization_id, topic_id)
