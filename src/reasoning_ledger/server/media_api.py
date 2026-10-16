"""Authenticated chunked attachment transport with the standard JSON body limit."""

from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, Response
from pydantic import Field

from .request_input import RequestInput
from .research_media import ResearchMedia


class MediaBegin(RequestInput):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    mime: str
    size_bytes: Annotated[int, Field(ge=1, le=8 * 1024 * 1024)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class MediaChunk(RequestInput):
    data_base64: Annotated[str, Field(min_length=1, max_length=43692)]


class MediaFinish(RequestInput):
    preview_text: Annotated[str, Field(max_length=24000)] = ""
    vision_base64: Annotated[str, Field(max_length=43692)] = ""


def install_routes(app, store, authenticated):
    media = ResearchMedia(store)
    User = Annotated[str, Depends(authenticated)]
    root = "/api/organizations/{organization_id}/topics/{topic_id}/media"

    @app.get(root)
    def files(organization_id: str, topic_id: str, user: User):
        return media.list(user, organization_id, topic_id)

    @app.post(root)
    def begin(organization_id: str, topic_id: str, body: MediaBegin, user: User):
        return media.begin(user, organization_id, topic_id, **body.model_dump())

    @app.put(root + "/{identity}/chunks/{index}")
    def chunk(organization_id: str, topic_id: str, identity: str, index: int, body: MediaChunk, user: User):
        return media.chunk(user, organization_id, topic_id, identity, index, body.data_base64)

    @app.post(root + "/{identity}/finish")
    def finish(organization_id: str, topic_id: str, identity: str, body: MediaFinish, user: User):
        return media.finish(user, organization_id, topic_id, identity, **body.model_dump())

    @app.get(root + "/{identity}/{sha256}/read")
    def read(organization_id: str, topic_id: str, identity: str, sha256: str, user: User, offset: int = 0, length: int = 12000):
        return media.read(user, organization_id, topic_id, identity, sha256, offset=offset, length=length)

    @app.get(root + "/{identity}/{sha256}/content")
    def content(organization_id: str, topic_id: str, identity: str, sha256: str, user: User):
        metadata = media.read(user, organization_id, topic_id, identity, sha256, length=1)
        mime, body = media.read(user, organization_id, topic_id, identity, sha256, binary=True)
        return Response(
            body,
            media_type=mime,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline; filename*=UTF-8''" + quote(metadata["name"], safe=""),
                "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
            },
        )
