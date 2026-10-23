"""Authenticated mathematical ledger and versioned research artifacts."""

from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, Response
from pydantic import Field

from ..service import RevisionObservations
from .request_input import RequestInput
from .research_artifacts import ResearchArtifacts
from .workbench_tools import MathematicalLedger


class LedgerOperation(RequestInput):
    name: str
    arguments: dict = Field(default_factory=dict)
    client_id: Annotated[str | None, Field(max_length=200)] = None
    project_id: str | None = None
    topic_id: str | None = None


class ArtifactWrite(RequestInput):
    path: Annotated[str, Field(max_length=200)]
    text: Annotated[str, Field(max_length=64000)]
    expected_sha256: str | None = None


class LedgerImport(RequestInput):
    project_id: str
    client_id: Annotated[str, Field(min_length=16, max_length=200)]
    records: Annotated[list[dict] | None, Field(max_length=10000)] = None
    source_label: Annotated[str | None, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")] = None


class HumanCheck(RequestInput):
    revision_id: Annotated[str, Field(min_length=1, max_length=200)]
    note: Annotated[str, Field(max_length=2000)] = ""


def install_routes(app, store, authenticated):
    from .ledger_human_checks import HumanChecks
    from .media_api import install_routes as install_media_routes

    install_media_routes(app, store, authenticated)

    ledger = MathematicalLedger(store)
    checks = HumanChecks(store)
    User = Annotated[str, Depends(authenticated)]
    prefix = "/api/organizations/{organization_id}/mathematics"

    @app.post(prefix + "/import")
    def import_snapshot(organization_id: str, body: LedgerImport, user: User):
        from .mathematical_import import import_archives, import_records

        if body.records is None:
            return import_archives(store, user, organization_id, body.project_id, body.client_id, body.source_label)
        return import_records(store, user, organization_id, body.project_id, body.records, body.client_id)

    @app.get(prefix)
    def status(organization_id: str, user: User, project_id: str | None = None, topic_id: str | None = None, q: str = ""):
        if topic_id:
            with store.transaction() as db:
                project_id = store._topic_access(db, user, organization_id, topic_id)[1]["id"]
        return ledger.execute(
            user,
            organization_id,
            "ledger_search" if q else "ledger_status",
            {"text": q} if q else {},
            project=project_id,
            topic=topic_id,
            agent=bool(topic_id),
        )

    @app.get(prefix + "/graph")
    def graph(organization_id: str, user: User, project_id: str | None = None, topic_id: str | None = None):
        if topic_id:
            with store.transaction() as db:
                project_id = store._topic_access(db, user, organization_id, topic_id)[1]["id"]
        return ledger.execute(user, organization_id, "ledger_graph", {}, project=project_id, topic=topic_id, agent=bool(topic_id))

    @app.get(prefix + "/{ledger_id}")
    def read(organization_id: str, ledger_id: str, user: User, history: bool = False):
        return ledger.execute(user, organization_id, "ledger_read", {"ledger_id": ledger_id, "history": history})

    @app.get(prefix + "/{ledger_id}/human-checks")
    def human_checks(organization_id: str, ledger_id: str, user: User):
        return checks.list(user, organization_id, ledger_id)

    @app.post(prefix + "/{ledger_id}/human-checks")
    def human_check(organization_id: str, ledger_id: str, body: HumanCheck, user: User):
        return checks.check(user, organization_id, ledger_id, body.revision_id, note=body.note)

    @app.post(prefix)
    def operate(organization_id: str, body: LedgerOperation, user: User):
        # Humans supply exact current revisions instead of inheriting an agent's reads.
        values = dict(body.arguments)
        expected = values.pop("expected_revision_id", None)
        observations = RevisionObservations()
        if expected and values.get("claim_id"):
            observations.note(values["claim_id"], expected)
        if expected and values.get("proof_id"):
            observations.note(values["proof_id"], expected)
        if expected and values.get("reference_id"):
            observations.note(values["reference_id"], expected)
        return ledger.execute(
            user,
            organization_id,
            body.name,
            values,
            client_id=body.client_id,
            project=body.project_id,
            topic=body.topic_id,
            observations=observations,
        )

    artifacts = ResearchArtifacts(store)
    artifact_prefix = "/api/organizations/{organization_id}/topics/{topic_id}/research-files"

    @app.get(artifact_prefix)
    def files(organization_id: str, topic_id: str, user: User):
        return artifacts.list(user, organization_id, topic_id)

    @app.get(artifact_prefix + "/read")
    def file(organization_id: str, topic_id: str, user: User, path: str, revision: int | None = None):
        return artifacts.read(user, organization_id, topic_id, path, revision, length=16000)

    @app.get(artifact_prefix + "/content")
    def file_content(organization_id: str, topic_id: str, user: User, path: str, revision: int, download: bool = False):
        body, checksum = artifacts.content(user, organization_id, topic_id, path, revision)
        disposition = "attachment" if download else "inline"
        return Response(body, media_type="text/plain; charset=utf-8", headers={
            "Content-Disposition": disposition + "; filename*=UTF-8''" + quote(path.rsplit("/", 1)[-1], safe=""),
            "ETag": '"' + checksum + '"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
        })

    @app.post(artifact_prefix)
    def write_file(organization_id: str, topic_id: str, body: ArtifactWrite, user: User):
        return artifacts.write(user, organization_id, topic_id, **body.model_dump())
