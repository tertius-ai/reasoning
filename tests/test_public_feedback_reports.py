"""Feedback reports: any member sends them, only owners and administrators read and resolve them."""

from __future__ import annotations

import pytest
from test_public_server import installation as installation
from test_public_server import invited

FAILED = {"at": 1792800000, "method": "PUT", "path": "/api/organizations/abc/topics/def/media/123/chunks/3", "status": 409,
          "detail": "an attachment chunk cannot be replaced"}
ERROR = {"at": 1792800001, "message": "TypeError: Cannot read properties of undefined",
         "stack": "at uploadAttachment (TopicMedia.js:1:4821)"}
CONTEXT = {"view": "topic β def · organization abc", "release": "development", "browser": "Mozilla/5.0 Chrome/141", "viewport": "1440×900",
           "requests": [FAILED], "errors": [ERROR]}


@pytest.fixture
def people(installation):
    _, store, owner_client, owner = installation
    _, member, _ = invited(installation, email="member@example.org")
    _, viewer, _ = invited(installation, role="viewer", email="viewer@example.org")
    _, admin, _ = invited(installation, role="admin", email="admin@example.org")
    root = f"/api/organizations/{owner['organization_id']}/feedback-reports"
    return {"store": store, "root": root, "owner": owner_client, "member": member, "viewer": viewer, "admin": admin}


def send(people, who, body, client_id, kind="bug", context=None):
    payload = {"kind": kind, "body": body, "client_id": client_id, **({"context": context} if context is not None else {})}
    return people[who].post(people["root"], json=payload)


def test_members_and_viewers_send_reports_that_only_administrators_read(people):
    first = send(people, "member", "The reply pointer stays highlighted.", "one", context=CONTEXT)
    assert first.status_code == 201, first.text
    assert send(people, "member", "The reply pointer stays highlighted.", "one", context=CONTEXT).json() == first.json()
    assert send(people, "member", "Something else", "one").status_code == 409
    assert send(people, "viewer", "Allow files in direct messages.", "two", kind="idea").status_code == 201
    for reader in ("member", "viewer"):
        assert people[reader].get(people["root"]).status_code == 403
    for reader in ("owner", "admin"):
        reports = people[reader].get(people["root"]).json()
        assert sorted(report["kind"] for report in reports) == ["bug", "idea"]
    bug = next(report for report in people["owner"].get(people["root"]).json() if report["kind"] == "bug")
    assert bug["reporter"]["name"] == "Member" and bug["context"] == CONTEXT and "client_id" not in bug
    idea = next(report for report in people["owner"].get(people["root"]).json() if report["kind"] == "idea")
    assert idea["context"] == {}, "nothing is attached unless the reporter chose to"


def test_administrators_resolve_and_reopen_reports(people):
    report = send(people, "member", "PDF extraction fails on scanned notes.", "pdf").json()
    path = people["root"] + f"/{report['id']}/resolution"
    assert people["member"].put(path, json={"resolved": True}).status_code == 403
    resolved = people["admin"].put(path, json={"resolved": True}).json()
    assert resolved["resolved_at"] and resolved["resolved_by"]
    assert people["admin"].put(path, json={"resolved": True}).json()["resolved_at"] == resolved["resolved_at"]
    assert people["owner"].put(path, json={"resolved": False}).json()["resolved_at"] is None
    assert people["owner"].put(people["root"] + "/" + "0" * 32 + "/resolution", json={"resolved": True}).status_code == 404


def test_reports_are_validated_and_rate_limited(people):
    assert send(people, "member", "   ", "blank").status_code == 422
    assert send(people, "member", "x" * 4001, "long").status_code == 422
    assert people["member"].post(people["root"], json={"kind": "praise", "body": "Nice", "client_id": "k"}).status_code == 422
    assert send(people, "member", "Bug", "extra", context={**CONTEXT, "messages": "secret"}).status_code == 422
    # Diagnostics are bounded, and a failed request never carries a body.
    assert send(people, "member", "Bug", "many", context={**CONTEXT, "requests": [FAILED] * 11}).status_code == 422
    assert send(people, "member", "Bug", "body", context={**CONTEXT, "requests": [{**FAILED, "body": "secret"}]}).status_code == 422
    assert send(people, "member", "Bug", "errors", context={**CONTEXT, "errors": [ERROR] * 6}).status_code == 422
    for index in range(10):
        assert send(people, "member", f"Report {index}", f"r{index}").status_code == 201
    assert send(people, "member", "One too many", "r10").status_code == 429
    assert send(people, "admin", "Others are not limited by it", "a1").status_code == 201
