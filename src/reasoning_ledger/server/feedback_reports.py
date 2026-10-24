"""Bug reports and ideas from members, read by the organization's owners and administrators.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

Every human member, viewers included, can send a report. A report holds what its author wrote and, only when they
chose to attach them after a preview, diagnostics: where they were (identifiers only; a direct conversation is never
named), the page's release, their browser and window size, the tab's last failed requests (method, path without its
query, status and the server's error text; never a body) and its last page errors. The server adds its own release.
"""

import json
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI
from pydantic import Field

from .request_input import RequestInput
from .store import Store, StoreError, _id, _row

REPORTS_PER_HOUR = 10
LIST_LIMIT = 200


class FeedbackReports:
    def __init__(self, store: Store, release: str | None = None):
        self.store = store
        self.release = release

    @staticmethod
    def _view(row) -> dict[str, Any]:
        item = dict(row)
        item["context"] = json.loads(item.pop("context_json"))
        item["reporter"] = {"id": item.pop("user_id"), "name": item.pop("reporter_name")}
        item.pop("client_id")
        return item

    def _read(self, db, organization_id: str, report_id: str) -> dict[str, Any]:
        return self._view(_row(db.execute(
            "SELECT f.*,u.name AS reporter_name FROM feedback_reports f JOIN users u ON u.id=f.user_id "
            "WHERE f.organization_id=? AND f.id=?", (organization_id, report_id),
        ).fetchone()))

    def send(self, user_id: str, organization_id: str, kind: str, body: str, client_id: str, context: dict | None) -> dict[str, Any]:
        stored = {**(context or {}), **({"server_release": self.release} if context is not None and self.release else {})}
        with self.store.transaction(write=True) as db:
            Store._require(db, user_id, organization_id)
            existing = db.execute(
                "SELECT id,kind,body FROM feedback_reports WHERE organization_id=? AND user_id=? AND client_id=?",
                (organization_id, user_id, client_id),
            ).fetchone()
            if existing is not None:
                if (existing["kind"], existing["body"]) != (kind, body):
                    raise StoreError(409, "client_id was already used for a different report")
                return {"id": existing["id"]}
            quota, now = "feedback:" + organization_id + ":" + user_id, int(time.time())
            db.execute("DELETE FROM auth_attempts WHERE scope=? AND attempted_at<?", (quota, now - 3600))
            if db.execute("SELECT COUNT(*) FROM auth_attempts WHERE scope=?", (quota,)).fetchone()[0] >= REPORTS_PER_HOUR:
                raise StoreError(429, "you can send at most 10 reports per hour")
            db.execute("INSERT INTO auth_attempts VALUES(?,?)", (quota, now))
            report_id = _id()
            db.execute(
                "INSERT INTO feedback_reports(id,organization_id,user_id,kind,body,context_json,client_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (report_id, organization_id, user_id, kind, body, json.dumps(stored), client_id, now),
            )
            self.store._audit(db, organization_id, user_id, "feedback.report", report_id)
            return {"id": report_id}

    def list(self, user_id: str, organization_id: str) -> list[dict[str, Any]]:
        with self.store.transaction() as db:
            Store._require(db, user_id, organization_id, admin=True)
            return [self._view(row) for row in db.execute(
                "SELECT f.*,u.name AS reporter_name FROM feedback_reports f JOIN users u ON u.id=f.user_id "
                "WHERE f.organization_id=? ORDER BY f.created_at DESC,f.id LIMIT ?", (organization_id, LIST_LIMIT),
            )]

    def resolve(self, user_id: str, organization_id: str, report_id: str, resolved: bool) -> dict[str, Any]:
        with self.store.transaction(write=True) as db:
            Store._require(db, user_id, organization_id, admin=True)
            report = self._read(db, organization_id, report_id)
            if bool(report["resolved_at"]) != resolved:
                db.execute(
                    "UPDATE feedback_reports SET resolved_at=?,resolved_by=? WHERE organization_id=? AND id=?",
                    (int(time.time()) if resolved else None, user_id if resolved else None, organization_id, report_id),
                )
                self.store._audit(db, organization_id, user_id, "feedback.resolve" if resolved else "feedback.reopen", report_id)
            return self._read(db, organization_id, report_id)


class FailedRequest(RequestInput):
    at: Annotated[int, Field(ge=0, le=2**40)]
    method: Annotated[str, Field(max_length=10)]
    path: Annotated[str, Field(max_length=300)]
    status: Annotated[int, Field(ge=0, le=599)]
    detail: Annotated[str, Field(max_length=300)]


class PageError(RequestInput):
    at: Annotated[int, Field(ge=0, le=2**40)]
    message: Annotated[str, Field(max_length=300)]
    stack: Annotated[str, Field(max_length=800)]


class FeedbackContext(RequestInput):
    view: Annotated[str, Field(max_length=300)] = ""
    release: Annotated[str, Field(max_length=80)] = ""
    browser: Annotated[str, Field(max_length=300)] = ""
    viewport: Annotated[str, Field(max_length=40)] = ""
    requests: Annotated[list[FailedRequest], Field(max_length=10)] = []
    errors: Annotated[list[PageError], Field(max_length=5)] = []


class FeedbackReport(RequestInput):
    kind: Literal["bug", "idea"]
    body: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]
    context: FeedbackContext | None = None


class FeedbackResolution(RequestInput):
    resolved: bool


def install_routes(app: FastAPI, store: Store, authenticated: Callable, release: str | None = None) -> None:
    User = Annotated[str, Depends(authenticated)]
    reports = FeedbackReports(store, release)
    root = "/api/organizations/{organization_id}/feedback-reports"

    @app.post(root, status_code=201)
    def send(organization_id: str, body: FeedbackReport, user: User):
        context = body.context.model_dump() if body.context is not None else None
        return reports.send(user, organization_id, body.kind, body.body, body.client_id, context)

    @app.get(root)
    def listing(organization_id: str, user: User):
        return reports.list(user, organization_id)

    @app.put(root + "/{report_id}/resolution")
    def resolve(organization_id: str, report_id: str, body: FeedbackResolution, user: User):
        return reports.resolve(user, organization_id, report_id, body.resolved)
