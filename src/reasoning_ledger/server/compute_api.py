"""Human compute administration and separately scoped worker protocol routes."""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field, SecretStr

from .compute_catalog import DEFAULT_RESOURCES
from .compute_environments import ComputeEnvironments
from .compute_scaler import ComputeScalers
from .compute_store import MAX_ATTACHMENTS, ComputeStore
from .lean_environments import MANIFEST_BYTES, TOOLCHAIN_BYTES
from .request_input import RequestInput as Input
from .store import StoreError


class Resources(Input):
    cpu: Annotated[int, Field(ge=1, le=64, strict=True)]
    memory_bytes: Annotated[int, Field(ge=64 * 1024**2, le=256 * 1024**3, strict=True)]
    disk_bytes: Annotated[int, Field(ge=8 * 1024**2, le=64 * 1024**3, strict=True)]
    pids: Annotated[int, Field(ge=16, le=4096, strict=True)]
    wall_time_seconds: Annotated[int, Field(ge=1, le=86400, strict=True)]
    artifact_bytes: Annotated[int, Field(ge=1, le=1024**3, strict=True)]


class Attachment(Input):
    identity: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]


class Job(Input):
    client_id: Annotated[str, Field(min_length=1, max_length=128)]
    image_id: Annotated[str, Field(min_length=1, max_length=64)]
    language: Literal["sage", "python", "julia", "gap", "pari", "lean"]
    program: Annotated[str, Field(min_length=1, max_length=16000)]
    resources: Resources = Field(default_factory=lambda: Resources(**DEFAULT_RESOURCES))
    attachments: Annotated[list[Attachment], Field(max_length=MAX_ATTACHMENTS)] | None = None

    def parameters(self, state: ComputeStore) -> dict:
        attachments = None if self.attachments is None else [item.model_dump() for item in self.attachments]
        return state.parameters(self.image_id, self.language, self.program, self.resources.model_dump(), attachments)


class Policy(Input):
    allowed_images: Annotated[list[Annotated[str, Field(min_length=1, max_length=64)]], Field(max_length=32)]
    monthly_cpu_seconds: Annotated[int, Field(ge=0, le=10**12, strict=True)]
    max_active_jobs: Annotated[int, Field(ge=1, le=4096, strict=True)] = 2
    max_pending_jobs: Annotated[int, Field(ge=1, le=1_000_000, strict=True)] | None = None
    requests_per_user_15_minutes: Annotated[int, Field(ge=1, le=1_000_000, strict=True)] | None = None


class EnvironmentRequest(Input):
    lean_toolchain: Annotated[str, Field(min_length=1, max_length=TOOLCHAIN_BYTES)]
    lake_manifest: Annotated[str, Field(min_length=1, max_length=MANIFEST_BYTES)]


class EnvironmentDecision(Input):
    decision: Literal["approved", "declined"]
    key: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]


class Worker(Input):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    image_ids: Annotated[list[Annotated[str, Field(min_length=1, max_length=64)]], Field(min_length=1, max_length=32)]
    resources: Resources


class Drain(Input):
    draining: Annotated[bool, Field(strict=True)]


class Scaler(Worker):
    maximum_workers: Annotated[int, Field(ge=1, le=64, strict=True)]
    lifetime_seconds: Annotated[int, Field(ge=3600, le=31_536_000, strict=True)] = 2_592_000


class ScalerRotation(Input):
    lifetime_seconds: Annotated[int, Field(ge=3600, le=31_536_000, strict=True)] = 2_592_000


class Enrollment(Worker):
    request_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)]
    worker_token: Annotated[SecretStr, Field(min_length=47, max_length=47)]


class Retirement(Input):
    cleanup_confirmed: Annotated[bool, Field(strict=True)]


class Reconcile(Input):
    elapsed_ms: Annotated[int, Field(ge=0, le=86400 * 1000, strict=True)]
    cleanup_confirmed: Annotated[bool, Field(strict=True)]


class Lease(Input):
    attempt_id: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")]
    lease: Annotated[SecretStr, Field(min_length=1, max_length=2048)]


class Claim(Input):
    claim_id: Annotated[str | None, Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")] = None
    supports_workflow_inputs: Annotated[bool, Field(strict=True)] = False
    supports_attachments: Annotated[bool, Field(strict=True)] = False


class Log(Lease):
    chunk_index: Annotated[int, Field(ge=0, le=4095, strict=True)]
    text: Annotated[str, Field(min_length=1, max_length=8192)]


class Outcome(Lease):
    status: Literal["succeeded", "failed", "cancelled"]
    elapsed_ms: Annotated[int, Field(ge=0, le=86400 * 1000, strict=True)]
    exit_code: Annotated[int | None, Field(ge=-255, le=255, strict=True)] = None
    error_code: (
        Literal[
            "execution_failed",
            "resource_limit",
            "wall_time_limit",
            "log_limit",
            "artifact_limit",
            "image_verification_failed",
            "input_verification_failed",
            "worker_stopped",
        ]
        | None
    ) = None


class Artifact(Lease):
    name: Annotated[str, Field(min_length=1, max_length=128)]
    size_bytes: Annotated[int, Field(ge=0, le=1024**3, strict=True)]
    digest: Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class ArtifactChunk(Lease):
    chunk_index: Annotated[int, Field(ge=0, le=32767, strict=True)]
    content_base64: Annotated[str, Field(max_length=43692)]


def install_routes(app: FastAPI, state: ComputeStore, authenticated: Callable) -> None:
    User = Annotated[str, Depends(authenticated)]
    scalers = ComputeScalers(state)
    app.state.compute_scalers = scalers
    environments = ComputeEnvironments(state.store)
    app.state.compute_environments = environments

    def worker_credential(request: Request) -> str:
        # Never accept browser sessions or silently fall back to cookies.
        scheme, separator, credential = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not separator or not credential or len(credential) > 2048:
            raise StoreError(401, "valid compute worker credential required")
        return credential

    Credential = Annotated[str, Depends(worker_credential)]

    @app.get("/api/organizations/{organization_id}/compute-scalers")
    def scaler_list(organization_id: str, user: User):
        return scalers.list(user, organization_id)

    @app.post("/api/organizations/{organization_id}/compute-scalers", status_code=201)
    def scaler_create(organization_id: str, body: Scaler, user: User):
        return scalers.create(
            user, organization_id, body.name, body.image_ids, body.resources.model_dump(), body.maximum_workers, body.lifetime_seconds
        )

    @app.post("/api/organizations/{organization_id}/compute-scalers/{scaler_id}/rotate")
    def scaler_rotate(organization_id: str, scaler_id: str, body: ScalerRotation, user: User):
        return scalers.rotate(user, organization_id, scaler_id, body.lifetime_seconds)

    @app.delete("/api/organizations/{organization_id}/compute-scalers/{scaler_id}", status_code=204)
    def scaler_revoke(organization_id: str, scaler_id: str, user: User):
        scalers.revoke(user, organization_id, scaler_id)

    @app.get("/api/compute-scaler/identity")
    def scaler_identity(credential: Credential):
        return scalers.identity(credential)

    @app.get("/api/compute-scaler/snapshot")
    def scaler_snapshot(credential: Credential):
        return scalers.snapshot(credential)

    @app.post("/api/compute-scaler/enrollments", status_code=201)
    def scaler_enroll(body: Enrollment, credential: Credential):
        return scalers.enroll(
            credential, body.request_id, body.name, body.image_ids, body.resources.model_dump(), body.worker_token.get_secret_value()
        )

    @app.get("/api/organizations/{organization_id}/compute-configuration")
    def configuration(organization_id: str, user: User):
        return state.configuration(user, organization_id)

    @app.put("/api/organizations/{organization_id}/compute-policy")
    def policy(organization_id: str, body: Policy, user: User):
        return state.set_policy(user, organization_id, body.allowed_images, body.monthly_cpu_seconds, body.max_active_jobs,
                                body.max_pending_jobs, body.requests_per_user_15_minutes)

    @app.get("/api/organizations/{organization_id}/compute-environments")
    def environment_list(organization_id: str, user: User):
        return environments.environments(user, organization_id)

    @app.post("/api/organizations/{organization_id}/compute-environments")
    def environment_request(organization_id: str, body: EnvironmentRequest, user: User):
        return environments.request(user, organization_id, body.lean_toolchain, body.lake_manifest)

    @app.get("/api/organizations/{organization_id}/compute-environments/{environment_id}")
    def environment(organization_id: str, environment_id: str, user: User):
        return environments.environment(user, organization_id, environment_id)

    @app.put("/api/organizations/{organization_id}/compute-environments/{environment_id}/decision")
    def environment_decision(organization_id: str, environment_id: str, body: EnvironmentDecision, user: User):
        return environments.decide(user, organization_id, environment_id, body.key, body.decision)

    @app.post("/api/organizations/{organization_id}/topics/{topic_id}/compute-jobs", status_code=202)
    def submit(organization_id: str, topic_id: str, body: Job, user: User):
        return state.enqueue(user, organization_id, topic_id, body.client_id, body.parameters(state))

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/compute-jobs")
    def jobs(organization_id: str, topic_id: str, user: User, limit: Annotated[int, Query(ge=1, le=50)] = 20,
             before: Annotated[int, Query(ge=1, le=2**63 - 1)] = 2**63 - 1, standalone: bool = False):
        return state.jobs(user, organization_id, topic_id, limit, before, standalone)

    @app.post("/api/organizations/{organization_id}/projects/{project_id}/compute-jobs", status_code=202)
    def project_submit(organization_id: str, project_id: str, body: Job, user: User):
        return state.enqueue_project(user, organization_id, project_id, body.client_id, body.parameters(state))

    @app.get("/api/organizations/{organization_id}/projects/{project_id}/compute-jobs")
    def project_jobs(organization_id: str, project_id: str, user: User,
                     limit: Annotated[int, Query(ge=1, le=50)] = 20,
                     before: Annotated[int, Query(ge=1, le=2**63 - 1)] = 2**63 - 1):
        return state.project_jobs(user, organization_id, project_id, limit, before)

    @app.get("/api/organizations/{organization_id}/compute-jobs/{job_id}")
    def job(organization_id: str, job_id: str, user: User):
        return state.job(user, organization_id, job_id)

    @app.get("/api/organizations/{organization_id}/compute-jobs/{job_id}/events")
    def events(organization_id: str, job_id: str, user: User, after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0):
        return state.events(user, organization_id, job_id, after)

    @app.post("/api/organizations/{organization_id}/compute-jobs/{job_id}/cancel")
    def cancel(organization_id: str, job_id: str, body: Input, user: User):
        return state.cancel(user, organization_id, job_id)

    @app.put("/api/organizations/{organization_id}/compute-jobs/{job_id}/reconciliation")
    def reconcile(organization_id: str, job_id: str, body: Reconcile, user: User):
        return state.reconcile(user, organization_id, job_id, body.elapsed_ms, body.cleanup_confirmed)

    @app.get("/api/organizations/{organization_id}/compute-jobs/{job_id}/artifacts")
    def artifacts(organization_id: str, job_id: str, user: User):
        return state.artifacts(user, organization_id, job_id)

    @app.get("/api/organizations/{organization_id}/compute-jobs/{job_id}/artifacts/{artifact_id}")
    def download(organization_id: str, job_id: str, artifact_id: str, user: User):
        meta, chunks = state.download(user, organization_id, job_id, artifact_id)
        return StreamingResponse(
            state.download_chunks(meta, chunks),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{meta["name"]}"', "Content-Length": str(meta["size_bytes"])},
        )

    @app.get("/api/organizations/{organization_id}/compute-workers")
    def workers(organization_id: str, user: User):
        return state.workers(user, organization_id)

    @app.get("/api/organizations/{organization_id}/compute-scaling-snapshot")
    def scaling_snapshot(organization_id: str, user: User):
        return state.scaling_snapshot(user, organization_id)

    @app.post("/api/organizations/{organization_id}/compute-workers", status_code=201)
    def register(organization_id: str, body: Worker, user: User):
        return state.register_worker(user, organization_id, body.name, body.image_ids, body.resources.model_dump())

    @app.patch("/api/organizations/{organization_id}/compute-workers/{worker_id}", status_code=204)
    def drain(organization_id: str, worker_id: str, body: Drain, user: User):
        state.change_worker(user, organization_id, worker_id, draining=body.draining)

    @app.put("/api/organizations/{organization_id}/compute-workers/{worker_id}/resources")
    def worker_resources(organization_id: str, worker_id: str, body: Resources, user: User):
        return state.change_worker_resources(user, organization_id, worker_id, body.model_dump())

    @app.delete("/api/organizations/{organization_id}/compute-workers/{worker_id}", status_code=204)
    def revoke(organization_id: str, worker_id: str, user: User):
        state.change_worker(user, organization_id, worker_id, revoke=True)

    @app.get("/api/worker/identity")
    def identity(credential: Credential):
        return state.worker_identity(credential)

    @app.get("/api/worker/status")
    def status(credential: Credential):
        return state.worker_status(credential)

    @app.post("/api/worker/drain")
    def drain_self(body: Input, credential: Credential):
        return state.drain_self(credential)

    @app.get("/api/worker/retirement")
    def retirement(credential: Credential):
        return state.worker_retirement(credential)

    @app.post("/api/worker/retirement")
    def retire(body: Retirement, credential: Credential):
        return state.retire_worker(credential, body.cleanup_confirmed)

    @app.post("/api/worker/claim")
    def claim(body: Claim, credential: Credential):
        return state.claim(
            credential, body.claim_id,
            supports_workflow_inputs=body.supports_workflow_inputs, supports_attachments=body.supports_attachments,
        )

    @app.post("/api/worker/jobs/{job_id}/heartbeat")
    def heartbeat(job_id: str, body: Lease, credential: Credential):
        return state.heartbeat(credential, job_id, body.attempt_id, body.lease.get_secret_value())

    @app.post("/api/worker/jobs/{job_id}/attachments/{identity}/chunks/{index}")
    def attachment_chunk(job_id: str, identity: str, index: int, body: Lease, credential: Credential):
        return state.attachment_chunk(credential, job_id, body.attempt_id, body.lease.get_secret_value(), identity, index)

    @app.post("/api/worker/jobs/{job_id}/logs", status_code=204)
    def log(job_id: str, body: Log, credential: Credential):
        state.append_log(credential, job_id, body.attempt_id, body.lease.get_secret_value(), body.chunk_index, body.text)

    @app.post("/api/worker/jobs/{job_id}/finish")
    def finish(job_id: str, body: Outcome, credential: Credential):
        return state.finish(
            credential,
            job_id,
            body.attempt_id,
            body.lease.get_secret_value(),
            body.status,
            body.elapsed_ms,
            body.exit_code,
            body.error_code,
        )

    @app.post("/api/worker/jobs/{job_id}/artifacts", status_code=201)
    def artifact(job_id: str, body: Artifact, credential: Credential):
        return state.begin_artifact(
            credential, job_id, body.attempt_id, body.lease.get_secret_value(), body.name, body.size_bytes, body.digest
        )

    @app.put("/api/worker/jobs/{job_id}/artifacts/{artifact_id}/chunks", status_code=204)
    def chunk(job_id: str, artifact_id: str, body: ArtifactChunk, credential: Credential):
        state.artifact_chunk(
            credential, job_id, body.attempt_id, body.lease.get_secret_value(), artifact_id, body.chunk_index, body.content_base64
        )

    @app.post("/api/worker/jobs/{job_id}/artifacts/{artifact_id}/commit")
    def commit(job_id: str, artifact_id: str, body: Lease, credential: Credential):
        return state.commit_artifact(credential, job_id, body.attempt_id, body.lease.get_secret_value(), artifact_id)
