"""Human workflow runs, bounded progress, approvals, cancellation, and retry."""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query
from pydantic import Field

from .request_input import RequestInput
from .workflow_runs import WorkflowRuns


class Input(RequestInput):
    client_id: Annotated[str, Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class Start(Input):
    version: Annotated[int, Field(ge=1, le=128, strict=True)]


class Approval(Input):
    attempt: Annotated[int, Field(ge=1, le=16, strict=True)]
    decision: Literal["approve", "reject"]


class Retry(Input):
    revision: Annotated[int, Field(ge=1, le=2**63 - 1, strict=True)]


def install_routes(app: FastAPI, state: WorkflowRuns, authenticated: Callable):
    User = Annotated[str, Depends(authenticated)]

    @app.get("/api/organizations/{organization_id}/workflow-configuration")
    def configuration(organization_id: str, user: User):
        with state.store.transaction() as db:
            state.store._require(db, user, organization_id)
        return {"enabled": True, "worker_running": app.state.workflow_worker.running, "inference_enabled": state.inference_enabled}

    @app.get("/api/organizations/{organization_id}/workflow-capacity")
    def capacity(organization_id: str, user: User):
        return state.capacity(user, organization_id)

    @app.get("/api/organizations/{organization_id}/workflow-runs/{run_id}/capacity-inspection")
    def inspect_capacity(organization_id: str, run_id: str, user: User):
        return state.inspect_capacity(user, organization_id, run_id)

    @app.post("/api/organizations/{organization_id}/workflows/{workflow_id}/runs", status_code=202)
    def start(organization_id: str, workflow_id: str, body: Start, user: User):
        return state.start(user, organization_id, workflow_id, body.version, body.client_id)

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/workflow-runs")
    def runs(
        organization_id: str,
        topic_id: str,
        user: User,
        before: Annotated[int, Query(ge=1, le=2**63 - 1)] = 2**63 - 1,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ):
        return state.list(user, organization_id, topic_id, before, limit)

    @app.get("/api/organizations/{organization_id}/workflow-runs/{run_id}")
    def run(organization_id: str, run_id: str, user: User):
        return state.get(user, organization_id, run_id)

    @app.get("/api/organizations/{organization_id}/workflow-runs/{run_id}/events")
    def events(organization_id: str, run_id: str, user: User, after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0):
        return state.events(user, organization_id, run_id, after)

    @app.get("/api/organizations/{organization_id}/workflow-runs/{run_id}/nodes/{node_id}/attempts")
    def attempts(organization_id: str, run_id: str, node_id: str, user: User):
        return state.attempts(user, organization_id, run_id, node_id)

    @app.get("/api/organizations/{organization_id}/workflow-runs/{run_id}/nodes/{node_id}/attempts/{attempt}/inputs")
    def inputs(organization_id: str, run_id: str, node_id: str, attempt: Annotated[int, Field(ge=1, le=16)], user: User):
        return state.inputs(user, organization_id, run_id, node_id, attempt)

    @app.post("/api/organizations/{organization_id}/workflow-runs/{run_id}/nodes/{node_id}/approval")
    def approve(organization_id: str, run_id: str, node_id: str, body: Approval, user: User):
        return state.approve(user, organization_id, run_id, node_id, body.attempt, body.decision, body.client_id)

    @app.post("/api/organizations/{organization_id}/workflow-runs/{run_id}/cancel")
    def cancel(organization_id: str, run_id: str, body: Input, user: User):
        return state.cancel(user, organization_id, run_id, body.client_id)

    @app.post("/api/organizations/{organization_id}/workflow-runs/{run_id}/retry")
    def retry(organization_id: str, run_id: str, body: Retry, user: User):
        return state.retry(user, organization_id, run_id, body.revision, body.client_id)

    @app.post("/api/organizations/{organization_id}/workflow-runs/{run_id}/release-capacity")
    def release_capacity(organization_id: str, run_id: str, body: Retry, user: User):
        return state.release_capacity(user, organization_id, run_id, body.revision, body.client_id)
