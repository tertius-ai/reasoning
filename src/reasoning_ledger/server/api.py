"""Public authenticated API, independent of the local-development workbench."""

import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from .bot_api import install_routes as install_bot_routes
from .bot_cloud_api import install_routes as install_cloud_bot_routes
from .bot_store import BotStore
from .compute_api import install_routes as install_compute_routes
from .compute_catalog import load_catalog
from .compute_store import ComputeStore
from .feedback_api import install_feedback_routes
from .inference_store import InferenceStore
from .inference_worker import DEFAULT_AGENT_CONCURRENCY, InferenceWorker
from .knowledge_store import KnowledgeStore
from .mail import MailConfig, MailStore, MailWorker
from .model_benchmarks import ModelBenchmarks, metadata_key
from .model_recommendations import recommendations
from .navigation_api import install_routes as install_navigation_routes
from .openrouter import OPENROUTER_URL, OpenRouter, ProviderError, provider_origin
from .request_input import RequestInput as Input
from .research_api import install_routes as install_research_routes
from .store import Store, StoreError
from .topic_events import SAFE_METHODS, TopicEvents
from .topic_events import install_routes as install_topic_event_routes
from .usage_api import install_routes as install_usage_routes
from .usage_store import UsageStore
from .webhook_api import install_routes as install_webhook_routes
from .webhook_store import WebhookStore
from .webhook_worker import WebhookWorker
from .work_api import install_routes as install_work_routes
from .work_jobs_api import install_routes as install_work_jobs_routes
from .work_maintenance import WorkLeaseMaintenance
from .workbench_api import install_routes as install_workbench_routes
from .workflow_api import install_routes as install_workflow_routes
from .workflow_run_api import install_routes as install_workflow_run_routes
from .workflow_runs import WorkflowRuns
from .workflow_store import WorkflowStore
from .workflow_worker import WorkflowWorker

SESSION_COOKIE = "reasoning_session"
CSRF_COOKIE = "reasoning_csrf"
APPLICATION_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
# The frontend build names every file under assets/ `<name>-<8-character content hash>.<extension>`.
HASHED_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*-[A-Za-z0-9_-]{8}\.[A-Za-z0-9]+")
IMMUTABLE_ASSET_CACHE = "public, max-age=31536000, immutable"
RELEASE_ID = re.compile(r"[a-f0-9]{64}")


def retained_asset_directories(frontend: Path) -> tuple[Path, ...]:
    """Asset directories of the other installed releases in the managed `<releases>/<release-id>/web` layout."""
    release = frontend.parent
    if frontend.name != "web" or not RELEASE_ID.fullmatch(release.name):
        return ()
    retained = []
    for sibling in sorted(release.parent.iterdir()):
        assets = sibling / "web" / "assets"
        if sibling != release and RELEASE_ID.fullmatch(sibling.name) and not sibling.is_symlink() and assets.is_dir():
            retained.append(assets)
    return tuple(retained)


class FrontendAssets(StaticFiles):
    """Serve this release's assets; a missing hashed file falls back to retained releases.

    A page loaded before an upgrade can still import the content-hashed chunks it references.
    """

    def __init__(self, directory: Path, retained: tuple[Path, ...] = ()):
        super().__init__(directory=directory)
        self.retained = [StaticFiles(directory=path) for path in retained]

    def lookup_path(self, path: str):
        found = super().lookup_path(path)
        if found[1] is None and HASHED_ASSET.fullmatch(path):
            for release in self.retained:
                found = release.lookup_path(path)
                if found[1] is not None:
                    break
        return found


@dataclass(frozen=True)
class Config:
    data_dir: Path
    public_url: str
    frontend_dir: Path | None = None
    inference_enabled: bool = True
    openrouter_url: str = OPENROUTER_URL
    compute_images: Path | None = None
    mail: MailConfig | None = None
    deployment_release: str | None = None
    agent_concurrency: int = DEFAULT_AGENT_CONCURRENCY
    topic_events: bool = True

    def __post_init__(self) -> None:
        if type(self.agent_concurrency) is not int or not 1 <= self.agent_concurrency <= 256:
            raise ValueError("agent concurrency must be between 1 and 256")
        if self.deployment_release is not None and (
            not isinstance(self.deployment_release, str) or not re.fullmatch(r"[a-f0-9]{64}", self.deployment_release)
        ):
            raise ValueError("deployment release must be a complete release manifest digest")
        provider_origin(self.openrouter_url)
        if any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in self.public_url):
            raise ValueError("public URL must not contain whitespace, control characters, or backslashes")
        parsed = urlsplit(self.public_url)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or "?" in self.public_url
            or "#" in self.public_url
        ):
            raise ValueError("public URL must be an origin without credentials, a path, query, or fragment")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("public URL requires HTTPS; HTTP is allowed only on localhost")
        # Accessing port also validates malformed/out-of-range ports.
        if parsed.port == 0:
            raise ValueError("public URL port must be between 1 and 65535")
        try:
            origin = AnyHttpUrl(self.public_url)
        except ValidationError:
            raise ValueError("public URL must contain a valid HTTP(S) host and port") from None
        # Match browser Origin serialization, including IDNA, IPv6, scheme/host
        # case, and omission of default ports. Validate raw input first so URL
        # normalization cannot hide credentials, paths, or empty query markers.
        object.__setattr__(self, "public_url", str(origin).rstrip("/"))
        if self.frontend_dir is not None:
            frontend = self.frontend_dir.resolve()
            if not (frontend / "organization.html").is_file() or not (frontend / "assets").is_dir():
                raise ValueError("frontend directory must contain a built organization.html and assets directory")
            object.__setattr__(self, "frontend_dir", frontend)

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https:")


class BodyLimit:
    def __init__(self, app: ASGIApp, maximum: int = 65536, worker_store=None):
        self.app, self.maximum, self.worker_store = app, maximum, worker_store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        maximum, allowance = self.maximum, "64 KiB"
        match = re.fullmatch(r"/api/work-worker/jobs/([a-f0-9]{32})/inference", scope.get("path", ""))
        if match and self.worker_store is not None:
            from .work_jobs import WorkJobs

            header = dict(scope.get("headers", [])).get(b"authorization", b"").decode("latin1")
            scheme, _, credential = header.partition(" ")
            try:
                if scheme.lower() != "bearer":
                    raise StoreError(401, "valid work worker credential required")
                with self.worker_store.transaction() as db:
                    worker = WorkJobs(self.worker_store)._worker(db, credential)
                    if not db.execute(
                        "SELECT 1 FROM work_jobs WHERE id=? AND worker_id=? AND status='leased'", (match[1], worker["id"])
                    ).fetchone():
                        raise StoreError(404, "assigned work job unavailable")
            except StoreError as exc:
                await JSONResponse({"detail": str(exc)}, status_code=exc.status)(scope, receive, send)
                return
            maximum, allowance = 32 * 1024 * 1024, "32 MiB"
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > maximum:
                await JSONResponse({"detail": "request body exceeds " + allowance}, status_code=413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


Name = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
Password = Annotated[SecretStr, Field(min_length=12, max_length=1024)]
Credential = Annotated[SecretStr, Field(min_length=1, max_length=2048)]
Transport = Literal["cookie", "bearer"]


class Account(Input):
    email: Annotated[str, Field(min_length=3, max_length=254)]

    @field_validator("email")
    @classmethod
    def email_format(cls, value: str) -> str:
        value = value.strip().lower()
        local, separator, domain = value.partition("@")
        if not local or separator != "@" or not domain or "@" in domain or any(c.isspace() for c in value):
            raise ValueError("invalid email address")
        return value


class Bootstrap(Account):
    setup_token: Credential
    name: Name
    password: Password
    organization_name: Name
    transport: Transport = "cookie"


class Login(Account):
    password: Annotated[SecretStr, Field(max_length=1024)]
    transport: Transport = "cookie"


class Organization(Input):
    name: Name


class ProjectCreation(Organization):
    visibility: Literal["organization", "private"] = "organization"
    member_ids: Annotated[list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]], Field(max_length=1000)] = []
    owner_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None

    @model_validator(mode="after")
    def private_owner(self):
        if self.visibility == "private" and self.owner_id is None:
            raise ValueError("private projects require an owner")
        if self.visibility == "organization" and (self.owner_id is not None or self.member_ids):
            raise ValueError("organization projects do not have a private member list")
        return self


class ProjectMember(Input):
    role: Literal["owner", "member"]


class EntityDeletion(Input):
    confirmation: Name
    plan_token: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    stop_active: Annotated[bool, Field(strict=True)] = False


class EmailVerificationRequest(Input):
    client_id: Annotated[str, Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class EmailVerificationConfirm(Input):
    token: SecretStr


class OrganizationCreation(Organization):
    credential_mode: Literal["new", "reuse", "none"]
    key_label: Name | None = None
    key: Annotated[SecretStr, Field(min_length=8, max_length=4096)] | None = None
    source_organization_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    source_key_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    allowed_models: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=10000)] = []
    monthly_budget_microusd: Annotated[int, Field(ge=0, le=10**15)] = 0

    @model_validator(mode="after")
    def complete_credential_choice(self):
        fresh = self.key_label is not None or self.key is not None or bool(self.allowed_models) or self.monthly_budget_microusd != 0
        source = self.source_organization_id is not None or self.source_key_id is not None
        if self.credential_mode == "new" and (
            self.key_label is None or self.key is None or self.monthly_budget_microusd <= 0 or source
        ):
            raise ValueError("a fresh key and positive budget are required")
        if self.credential_mode == "reuse" and (self.source_organization_id is None or self.source_key_id is None or fresh):
            raise ValueError("an existing organization key is required without fresh-key fields")
        if self.credential_mode == "none" and (fresh or source):
            raise ValueError("no-AI setup cannot include credential or policy fields")
        return self


class Channel(Organization):
    project_id: Annotated[str, Field(min_length=1, max_length=64)]


class Topic(Organization):
    channel_id: Annotated[str, Field(min_length=1, max_length=64)]
    kind: Literal["alpha", "beta", "gamma"]
    default_model: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    default_effort: Annotated[str, Field(min_length=1, max_length=20)] | None = None
    knowledge_tools: Annotated[list[Literal["ledger", "bibliography", "journal", "workflows"]], Field(max_length=4)] | None = None
    agent_tools: Annotated[list[Literal["history", "arxiv", "web", "knowledge", "workflows", "compute"]], Field(max_length=6)] | None = None


class Message(Input):
    content: Annotated[str, Field(min_length=1, max_length=16000, pattern=r"\S")]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]


class ModelFavorite(Input):
    model_id: Annotated[str, Field(min_length=1, max_length=200)]
    favorite: Annotated[bool, Field(strict=True)]


class MessageEdit(Input):
    content: Annotated[str, Field(min_length=1, max_length=16000, pattern=r"\S")]
    revision: Annotated[int, Field(ge=0)]


class Invitation(Account):
    role: Literal["admin", "member", "viewer"] = "member"
    lifetime_seconds: Annotated[int, Field(ge=60, le=604800)] = 604800
    send_email: bool = False
    client_id: Annotated[str, Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")] | None = None


class BatchInvitations(Input):
    emails: Annotated[list[Annotated[str, Field(min_length=3, max_length=320)]], Field(min_length=1, max_length=100)]
    role: Literal["admin", "member", "viewer"] = "member"
    lifetime_seconds: Annotated[int, Field(ge=60, le=604800)] = 604800
    client_id: Annotated[str, Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class AcceptInvitation(Input):
    token: Credential
    name: Name | None = None
    password: Password | None = None
    transport: Transport = "cookie"


class MemberRole(Input):
    role: Literal["owner", "admin", "member", "viewer"]


class ProviderKey(Input):
    provider: Literal["openrouter"] = "openrouter"
    label: Name
    key: Annotated[SecretStr, Field(min_length=8, max_length=4096)]
    allow_all_text_models: bool = False


class Policy(Input):
    allowed_models: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=10000)] = []
    monthly_budget_microusd: Annotated[int, Field(ge=0, le=10**15)]


class Subscription(Input):
    subscribed: bool


class ReadCursor(Input):
    sequence: Annotated[int, Field(ge=0, le=2**63 - 1)]


class InferenceRequest(Input):
    models: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(min_length=1, max_length=4)]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]
    max_completion_tokens: Annotated[int | None, Field(ge=16, le=2**31 - 1, strict=True)] = None
    context_message_limit: Annotated[int, Field(ge=1, le=100, strict=True)] = 50
    through_sequence: Annotated[int | None, Field(ge=1, le=2**63 - 1, strict=True)] = None
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None


class ReconciledCost(Input):
    actual_microusd: Annotated[int, Field(ge=0, le=10**15, strict=True)]


class ImpactProfile(Input):
    model: Name
    input_kwh_per_million: Annotated[float, Field(ge=0, le=10**6, allow_inf_nan=False, strict=True)]
    output_kwh_per_million: Annotated[float, Field(ge=0, le=10**6, allow_inf_nan=False, strict=True)]
    location: Name
    grid_grams_co2e_per_kwh: Annotated[float | None, Field(ge=0, le=2000, allow_inf_nan=False, strict=True)] = None
    source: Annotated[str, Field(min_length=1, max_length=1000)]


class ImpactProfiles(Input):
    profiles: Annotated[list[ImpactProfile], Field(max_length=100)]


class KnowledgeWrite(Input):
    client_id: Annotated[str, Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
    kind: Literal["ledger", "bibliography", "journal"]
    title: Annotated[str, Field(min_length=1, max_length=300, pattern=r"\S")]
    body: Annotated[str, Field(min_length=1, max_length=64_000, pattern=r"\S")]
    state: Annotated[str, Field(min_length=1, max_length=20)]
    scope_kind: Literal["organization", "project", "topic"] | None = None
    scope_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    expected_revision: Annotated[int, Field(ge=1, le=9_999, strict=True)] | None = None
    unpublish: Annotated[bool, Field(strict=True)] = False


class RoutingWrite(Input):
    strategy: Literal["recommended", "cheapest", "latency", "throughput"]
    zdr: Annotated[bool, Field(strict=True)] = False
    region: Literal["global", "us", "eu"] = "global"


def create_app(config: Config, *, provider: OpenRouter | None = None, mail_provider=None, webhook_transport=None) -> FastAPI:
    store = Store(config.data_dir)
    inference = InferenceStore(store)
    usage = UsageStore(store)
    knowledge = KnowledgeStore(store)
    from .research_blog import install_routes as install_blog_routes

    compute = ComputeStore(store, load_catalog(config.compute_images))
    provider = provider or OpenRouter(config.openrouter_url)
    benchmarks = ModelBenchmarks(provider)
    worker = InferenceWorker(inference, provider, concurrency=config.agent_concurrency)
    mail = MailStore(store, config.mail, config.public_url)
    mail_worker = MailWorker(mail, mail_provider)
    workflows = WorkflowStore(store)
    workflow_runs = WorkflowRuns(workflows, compute, inference, inference_enabled=config.inference_enabled)
    workflow_worker = WorkflowWorker(workflow_runs, provider)
    worker.compute, worker.workflows = compute, workflow_runs
    webhooks = WebhookStore(store)
    webhook_worker = WebhookWorker(webhooks, webhook_transport)
    work_maintenance = WorkLeaseMaintenance(store, compute)
    topic_events = TopicEvents(store, inference, enabled=config.topic_events)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        mail_started = False
        webhooks_started = False
        topic_events.chain_shutdown_signals()
        try:
            work_maintenance.start()
            if config.inference_enabled:
                worker.start()
            if config.mail:
                mail_worker.start()
                mail_started = True
            workflow_worker.start()
            webhook_worker.start()
            webhooks_started = True
            yield
        finally:
            topic_events.close()
            topic_events.restore_shutdown_signals()
            try:
                work_maintenance.stop()
            finally:
                stop_other_workers(mail_started, webhooks_started)

    def stop_other_workers(mail_started, webhooks_started):
        try:
            if webhooks_started:
                webhook_worker.stop()
        finally:
            try:
                workflow_worker.stop()
            finally:
                try:
                    if mail_started:
                        mail_worker.stop()
                finally:
                    if config.inference_enabled:
                        worker.stop()

    session_cookie = "__Host-" + SESSION_COOKIE if config.secure else SESSION_COOKIE
    csrf_cookie = "__Host-" + CSRF_COOKIE if config.secure else CSRF_COOKIE
    app = FastAPI(title="Reasoning public server", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.config = config
    app.state.inference = inference
    app.state.inference_worker = worker
    app.state.knowledge = knowledge
    app.state.compute = compute
    app.state.mail = mail
    app.state.mail_worker = mail_worker
    app.state.workflows = workflows
    app.state.workflow_runs = workflow_runs
    app.state.workflow_worker = workflow_worker
    app.state.webhooks = webhooks
    app.state.webhook_worker = webhook_worker
    app.state.topic_events = topic_events
    app.add_middleware(BodyLimit, worker_store=store)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin is not None and origin != config.public_url:
            response = JSONResponse({"detail": "origin not allowed"}, status_code=403)
        elif (
            request.method in {"POST", "PUT", "PATCH"}
            and request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json"
        ):
            response = JSONResponse({"detail": "application/json required"}, status_code=415)
        else:
            response = await call_next(request)
        # Only successful content-hashed public build files may be cached; everything else stays private.
        versioned = (
            config.frontend_dir is not None
            and request.method in {"GET", "HEAD"}
            and response.status_code in {200, 304}
            and request.url.path.startswith("/assets/")
            and HASHED_ASSET.fullmatch(request.url.path.removeprefix("/assets/")) is not None
        )
        # no-transform keeps compressing proxies (Caddy's encode) from holding event-stream bytes back.
        streaming = response.headers.get("content-type", "").startswith("text/event-stream")
        response.headers["Cache-Control"] = IMMUTABLE_ASSET_CACHE if versioned else "no-store, no-transform" if streaming else "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # A route's own policy (such as sandboxed media content) is stricter than the application policy.
        response.headers.setdefault("Content-Security-Policy", APPLICATION_CSP)
        if config.secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.method not in SAFE_METHODS and response.status_code < 400:
            topic_events.nudge(request.url.path)
        return response

    @app.exception_handler(StoreError)
    async def store_error(_request: Request, exc: StoreError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        # Pydantic's default response includes input values, including passwords and API keys.
        errors = [
            {"loc": ("body",) if error["type"] == "extra_forbidden" else error["loc"], "type": error["type"]} for error in exc.errors()
        ]
        return JSONResponse({"detail": errors}, status_code=422)

    def credentials(request: Request, *, optional: bool = False) -> tuple[str, str] | None:
        authorization = request.headers.get("authorization")
        if authorization is not None:
            scheme, separator, access = authorization.partition(" ")
            if scheme.lower() != "bearer" or not separator or not access or len(access) > 2048:
                raise StoreError(401, "invalid authorization credential")
            session = store.session(access)
        else:
            access = request.cookies.get(session_cookie)
            if not access:
                if optional:
                    return None
                raise StoreError(401, "sign-in required")
            csrf = None
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if request.headers.get("origin") != config.public_url:
                    raise StoreError(403, "same-origin request required")
                csrf = request.headers.get("x-csrf-token")
                if not csrf:
                    raise StoreError(403, "CSRF credential required")
            session = store.session(access, csrf)
        return session["user_id"], access

    def authenticated(request: Request) -> str:
        result = credentials(request)
        assert result is not None
        return result[0]

    User = Annotated[str, Depends(authenticated)]
    install_research_routes(app, inference, compute, workflow_runs, authenticated, enabled=config.inference_enabled)
    install_workbench_routes(app, store, authenticated)
    install_work_routes(app, store, authenticated)
    install_work_jobs_routes(app, store, authenticated, provider)
    install_compute_routes(app, compute, authenticated)
    from .bot_diagnostics import deployment_identity

    install_bot_routes(app, BotStore(store), authenticated, compute=compute,
                       deployment=deployment_identity(config.frontend_dir, config.deployment_release))
    install_cloud_bot_routes(app, store, config, authenticated)
    install_webhook_routes(app, webhooks, authenticated, webhook_worker)
    install_workflow_routes(app, app.state.workflows, authenticated)
    install_workflow_run_routes(app, workflow_runs, authenticated)
    install_usage_routes(app, usage, authenticated)
    install_feedback_routes(app, store, authenticated)
    install_navigation_routes(app, store, authenticated)
    install_topic_event_routes(app, topic_events, credentials)

    def session_response(result: dict, response: Response, transport: Transport) -> dict:
        if transport == "bearer":
            return result
        access, csrf = result["access_token"], result["csrf_token"]
        response.set_cookie(session_cookie, access, max_age=Store.session_lifetime, httponly=True, secure=config.secure, samesite="strict")
        response.set_cookie(csrf_cookie, csrf, max_age=Store.session_lifetime, httponly=False, secure=config.secure, samesite="strict")
        return {key: value for key, value in result.items() if key != "access_token"}

    @app.get("/api/health")
    def health(response: Response):
        if config.deployment_release is not None:
            response.headers["X-Reasoning-Release"] = config.deployment_release
        return {
            "status": "ok",
            "initialized": store.initialized(),
            **({"mail_worker_running": mail_worker.running} if config.mail is not None else {}),
        }

    @app.exception_handler(ProviderError)
    async def provider_error(_request: Request, exc: ProviderError):
        return JSONResponse({"detail": exc.code}, status_code=409 if exc.code == "context_limit" else 502)

    @app.post("/api/setup", status_code=201)
    def setup(body: Bootstrap, response: Response):
        result = store.bootstrap(
            body.setup_token.get_secret_value(), body.email, body.name, body.password.get_secret_value(), body.organization_name
        )
        return session_response(result, response, body.transport)

    @app.post("/api/auth/login")
    def login(body: Login, request: Request, response: Response):
        peer = request.client.host if request.client else "unknown"
        return session_response(store.login(body.email, body.password.get_secret_value(), peer), response, body.transport)

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request, response: Response, _user: User):
        credential = credentials(request)
        assert credential is not None
        store.logout(credential[1])
        response.delete_cookie(session_cookie, secure=config.secure, httponly=True, samesite="strict")
        response.delete_cookie(csrf_cookie, secure=config.secure, samesite="strict")

    @app.get("/api/auth/me")
    def me(user: User):
        return store.me(user)

    @app.get("/api/auth/model-favorites")
    def model_favorites(user: User):
        from .model_preferences import favorites

        return favorites(store, user)

    @app.put("/api/auth/model-favorites")
    def model_favorite(body: ModelFavorite, user: User):
        from .model_preferences import favorites

        return favorites(store, user, **body.model_dump())

    @app.get("/api/auth/email-verification")
    def verification_status(user: User):
        return mail.verification.status(user)

    @app.post("/api/auth/email-verification")
    def request_verification(body: EmailVerificationRequest, user: User):
        return mail.verification.enqueue(user, body.client_id)

    @app.post("/api/auth/email-verification/confirm")
    def confirm_verification(body: EmailVerificationConfirm, user: User):
        return mail.verification.verify(user, body.token.get_secret_value())

    @app.get("/api/organizations")
    def organizations(user: User):
        return store.organizations(user)

    @app.post("/api/organizations", status_code=201)
    def create_organization(body: OrganizationCreation, user: User):
        return store.create_organization(
            user,
            body.name,
            credential_mode=body.credential_mode,
            key_label=body.key_label,
            key=body.key.get_secret_value() if body.key is not None else None,
            source_organization_id=body.source_organization_id,
            source_key_id=body.source_key_id,
            allowed_models=body.allowed_models,
            monthly_budget_microusd=body.monthly_budget_microusd,
        )

    @app.get("/api/organizations/{organization_id}/hierarchy")
    def hierarchy(organization_id: str, user: User):
        return store.hierarchy(user, organization_id)

    @app.post("/api/organizations/{organization_id}/projects", status_code=201)
    def project(organization_id: str, body: ProjectCreation, user: User):
        return store.create_entity(
            user,
            organization_id,
            "projects",
            body.name,
            visibility=body.visibility,
            member_ids=body.member_ids,
            owner_id=body.owner_id,
        )

    @app.patch("/api/organizations/{organization_id}/projects/{project_id}")
    def rename_project(organization_id: str, project_id: str, body: Organization, user: User):
        return store.rename_entity(user, organization_id, "projects", project_id, body.name)

    @app.get("/api/organizations/{organization_id}/projects/{project_id}/members")
    def project_members(organization_id: str, project_id: str, user: User):
        return store.project_members(user, organization_id, project_id)

    @app.put("/api/organizations/{organization_id}/projects/{project_id}/members/{member_id}", status_code=204)
    def put_project_member(organization_id: str, project_id: str, member_id: str, body: ProjectMember, user: User):
        store.change_project_member(user, organization_id, project_id, member_id, body.role)

    @app.delete("/api/organizations/{organization_id}/projects/{project_id}/members/{member_id}", status_code=204)
    def delete_project_member(organization_id: str, project_id: str, member_id: str, user: User):
        store.change_project_member(user, organization_id, project_id, member_id, None)

    @app.post("/api/organizations/{organization_id}/channels", status_code=201)
    def channel(organization_id: str, body: Channel, user: User):
        return store.create_entity(user, organization_id, "channels", body.name, body.project_id)

    @app.patch("/api/organizations/{organization_id}/channels/{channel_id}")
    def rename_channel(organization_id: str, channel_id: str, body: Organization, user: User):
        return store.rename_entity(user, organization_id, "channels", channel_id, body.name)

    @app.get("/api/organizations/{organization_id}/{kind}/{entity_id}/deletion")
    def entity_deletion_plan(organization_id: str, kind: Literal["projects", "channels", "topics"], entity_id: str, user: User):
        return store.deletion_plan(user, organization_id, kind, entity_id)

    @app.delete("/api/organizations/{organization_id}/projects/{entity_id}", status_code=204)
    def delete_project(organization_id: str, entity_id: str, body: EntityDeletion, user: User):
        store.delete_entity(user, organization_id, "projects", entity_id, body.confirmation, body.plan_token, stop_active=body.stop_active)

    @app.delete("/api/organizations/{organization_id}/channels/{entity_id}", status_code=204)
    def delete_channel(organization_id: str, entity_id: str, body: EntityDeletion, user: User):
        store.delete_entity(user, organization_id, "channels", entity_id, body.confirmation, body.plan_token, stop_active=body.stop_active)

    @app.delete("/api/organizations/{organization_id}/topics/{entity_id}", status_code=204)
    def delete_topic(organization_id: str, entity_id: str, body: EntityDeletion, user: User):
        store.delete_entity(user, organization_id, "topics", entity_id, body.confirmation, body.plan_token, stop_active=body.stop_active)

    @app.post("/api/organizations/{organization_id}/topics", status_code=201)
    def topic(organization_id: str, body: Topic, user: User):
        return store.create_entity(
            user,
            organization_id,
            "topics",
            body.name,
            body.channel_id,
            body.kind,
            body.default_model,
            body.knowledge_tools,
            agent_tools=body.agent_tools,
            default_effort=body.default_effort,
        )

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/messages")
    def messages(
        organization_id: str,
        topic_id: str,
        user: User,
        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        before: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None,
    ):
        return store.messages(user, organization_id, topic_id, after, limit, before)

    @app.post("/api/organizations/{organization_id}/topics/{topic_id}/messages", status_code=201)
    def post_message(organization_id: str, topic_id: str, body: Message, user: User):
        return store.post_message(user, organization_id, topic_id, body.content, body.client_id)

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/message-updates")
    def message_updates(organization_id: str, topic_id: str, user: User,
                        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0):
        return store.message_updates(user, organization_id, topic_id, after)

    @app.patch("/api/organizations/{organization_id}/topics/{topic_id}/messages/{message_id}")
    def edit_message(organization_id: str, topic_id: str, message_id: str, body: MessageEdit, user: User):
        return store.edit_message(user, organization_id, topic_id, message_id, body.content, body.revision)

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/messages/{message_id}/history")
    def message_history(organization_id: str, topic_id: str, message_id: str, user: User):
        return store.message_history(user, organization_id, topic_id, message_id)

    @app.put("/api/organizations/{organization_id}/topics/{topic_id}/subscription", status_code=204)
    def subscription(organization_id: str, topic_id: str, body: Subscription, user: User):
        store.topic_preference(user, organization_id, topic_id, subscribed=body.subscribed)

    @app.put("/api/organizations/{organization_id}/topics/{topic_id}/read", status_code=204)
    def read_topic(organization_id: str, topic_id: str, body: ReadCursor, user: User):
        store.topic_preference(user, organization_id, topic_id, sequence=body.sequence)

    def catalog_access(user: str, organization_id: str):
        with store.transaction() as db:
            store._require(db, user, organization_id)

    @app.get("/api/organizations/{organization_id}/inference-configuration")
    async def inference_configuration(organization_id: str, user: User):
        await run_in_threadpool(catalog_access, user, organization_id)
        models = await provider.prices()
        configuration = await run_in_threadpool(inference.configuration, user, organization_id, models=[model["id"] for model in models])
        return {**configuration,
                "enabled": config.inference_enabled, "worker_running": worker.running}

    install_blog_routes(app, store, authenticated, frontend=config.frontend_dir)

    @app.get("/api/organizations/{organization_id}/model-prices")
    async def model_prices(organization_id: str, user: User):
        # Membership is checked before any external request. The public catalog needs no key.
        await run_in_threadpool(catalog_access, user, organization_id)
        models = await provider.prices(include_routing=True)
        policy = await run_in_threadpool(inference.configuration, user, organization_id, models=[model["id"] for model in models])
        pinned = await run_in_threadpool(usage.recommendation_preferences, user, organization_id)
        return {
            "models": models,
            "allowed_models": [model["id"] for model in models],
            "recommendations": recommendations(models, pinned=pinned),
            "fetched_at": getattr(provider, "prices_fetched_at", None) or int(time.time()),
            "routing": policy["routing"]["strategy"],
            "source": "OpenRouter public model catalog",
        }

    @app.get("/api/organizations/{organization_id}/model-benchmarks")
    async def model_benchmarks(organization_id: str, user: User):
        credential = await run_in_threadpool(metadata_key, store, user, organization_id)
        return await benchmarks.fetch(organization_id, credential)

    @app.put("/api/organizations/{organization_id}/routing")
    def routing(organization_id: str, body: RoutingWrite, user: User):
        return inference.set_routing(user, organization_id, **body.model_dump())

    @app.get("/api/organizations/{organization_id}/usage")
    def organization_usage(organization_id: str, user: User, project_id: str | None = None):
        return usage.totals(user, organization_id, project_id)

    @app.get("/api/organizations/{organization_id}/knowledge")
    def knowledge_entries(
        organization_id: str,
        user: User,
        kind: Literal["ledger", "bibliography", "journal"] | None = None,
        q: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
        before: Annotated[str | None, Query(max_length=45)] = None,
        project_id: Annotated[str | None, Query(pattern=r"^[a-f0-9]{32}$")] = None,
    ):
        return knowledge.list(user, organization_id, kind=kind, query=q, limit=limit, before=before, project_id=project_id)

    @app.get("/api/organizations/{organization_id}/knowledge/{entry_id}")
    def knowledge_entry(
        organization_id: str,
        entry_id: str,
        user: User,
        revision: Annotated[int | None, Query(ge=1, le=10_000)] = None,
    ):
        return knowledge.get(user, organization_id, entry_id, revision)

    @app.post("/api/organizations/{organization_id}/knowledge", status_code=201)
    def create_knowledge(organization_id: str, body: KnowledgeWrite, user: User):
        return knowledge.create(user, organization_id, **body.model_dump(exclude={"expected_revision"}))

    @app.put("/api/organizations/{organization_id}/knowledge/{entry_id}")
    def revise_knowledge(organization_id: str, entry_id: str, body: KnowledgeWrite, user: User):
        if body.scope_kind is not None or body.scope_id is not None or body.expected_revision is None:
            raise StoreError(400, "revisions require expected_revision without a new scope")
        return knowledge.revise(user, organization_id, entry_id, **body.model_dump(exclude={"scope_kind", "scope_id"}))

    @app.get("/api/organizations/{organization_id}/impact-profiles")
    def impact_profiles(organization_id: str, user: User):
        return {"profiles": usage.profiles(user, organization_id)}

    @app.put("/api/organizations/{organization_id}/impact-profiles")
    def update_impact_profiles(organization_id: str, body: ImpactProfiles, user: User):
        return {"profiles": usage.set_profiles(user, organization_id, [profile.model_dump() for profile in body.profiles])}

    @app.post("/api/organizations/{organization_id}/topics/{topic_id}/inference-runs", status_code=202)
    async def request_models(organization_id: str, topic_id: str, body: InferenceRequest, user: User):
        from .inference_timing import Spans

        spans = Spans("admission")
        parameters = inference.parameters(body.models, body.max_completion_tokens, body.context_message_limit,
                                           body.through_sequence, effort=body.effort)
        existing = await run_in_threadpool(inference.admit, user, organization_id, topic_id, body.client_id, parameters)
        if existing is not None:
            return existing
        spans.mark("admission_check")
        if not config.inference_enabled:
            raise StoreError(503, "inference is disabled on this installation")
        quotes = await provider.quotes(body.models)
        spans.mark("catalog_quote")
        # Later reviewer/workflow admission reads the most recent provider model list.
        inference.store._model_catalog = list(provider.catalog_models)
        return await run_in_threadpool(inference.enqueue, user, organization_id, topic_id, body.client_id, parameters, quotes,
                                       spans=spans)

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/inference-runs")
    def model_runs(organization_id: str, topic_id: str, user: User, limit: Annotated[int, Query(ge=1, le=50)] = 20):
        return inference.runs(user, organization_id, topic_id, limit=limit)

    @app.get("/api/organizations/{organization_id}/inference-runs/{run_id}")
    def model_run(organization_id: str, run_id: str, user: User):
        return inference.run(user, organization_id, run_id)

    @app.get("/api/organizations/{organization_id}/inference-runs/{run_id}/events")
    def model_events(organization_id: str, run_id: str, user: User, after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0):
        return inference.events(user, organization_id, run_id, after)

    @app.post("/api/organizations/{organization_id}/inference-runs/{run_id}/cancel")
    def cancel_model(organization_id: str, run_id: str, body: Input, user: User):
        return inference.cancel(user, organization_id, run_id)

    @app.put("/api/organizations/{organization_id}/inference-runs/{run_id}/cost", status_code=204)
    def reconcile_cost(organization_id: str, run_id: str, body: ReconciledCost, user: User):
        inference.reconcile(user, organization_id, run_id, body.actual_microusd)

    @app.get("/api/organizations/{organization_id}/search")
    def search(
        organization_id: str,
        user: User,
        q: Annotated[str, Query(min_length=1, max_length=200)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        return store.search(user, organization_id, q, limit)

    @app.get("/api/organizations/{organization_id}/members")
    def members(organization_id: str, user: User):
        return store.members(user, organization_id)

    @app.get("/api/organizations/{organization_id}/presence")
    def presence(organization_id: str, user: User):
        return store.presence(user, organization_id)

    @app.patch("/api/organizations/{organization_id}/members/{member_id}", status_code=204)
    def change_member(organization_id: str, member_id: str, body: MemberRole, user: User):
        store.change_member(user, organization_id, member_id, body.role)

    @app.delete("/api/organizations/{organization_id}/members/{member_id}", status_code=204)
    def remove_member(organization_id: str, member_id: str, user: User):
        store.change_member(user, organization_id, member_id, None)

    @app.post("/api/organizations/{organization_id}/invitations", status_code=201)
    def invite(organization_id: str, body: Invitation, user: User):
        if body.send_email:
            return mail.enqueue(user, organization_id, body.email, body.role, body.lifetime_seconds, body.client_id)
        if body.client_id is not None:
            raise StoreError(400, "client_id is supported only for email invitations")
        return store.invite(user, organization_id, body.email, body.role, body.lifetime_seconds)

    @app.get("/api/organizations/{organization_id}/invitation-mail")
    def invitation_mail(organization_id: str, user: User):
        return {**mail.configuration(user, organization_id), "worker_running": mail_worker.running}

    @app.post("/api/organizations/{organization_id}/invitations/batch")
    def batch_invite(organization_id: str, body: BatchInvitations, user: User):
        return mail.enqueue_batch(user, organization_id, body.emails, body.role, body.lifetime_seconds, body.client_id)

    @app.get("/api/organizations/{organization_id}/provider-key-usage")
    def provider_key_usage(organization_id: str, user: User):
        return {"keys": usage.key_totals(user, organization_id),
                "scope": "All-time Reasoning provider calls only; excludes use outside this installation and bot CLI billing."}

    @app.get("/api/organizations/{organization_id}/invitations")
    def invitations(organization_id: str, user: User):
        return store.invitations(user, organization_id)

    @app.delete("/api/organizations/{organization_id}/invitations/{invitation_id}", status_code=204)
    def revoke_invitation(organization_id: str, invitation_id: str, user: User):
        store.revoke_invitation(user, organization_id, invitation_id)

    @app.post("/api/organizations/{organization_id}/invitations/{invitation_id}/resend", status_code=201)
    def resend_invitation(organization_id: str, invitation_id: str, body: EmailVerificationRequest, user: User):
        with store.transaction() as db:
            store._require(db, user, organization_id, admin=True)
            invite = db.execute("SELECT email,role FROM invitations WHERE id=? AND organization_id=?",
                                (invitation_id, organization_id)).fetchone()
            if invite is None:
                raise StoreError(404, "invitation unavailable")
        return mail.enqueue(user, organization_id, invite["email"], invite["role"], 7 * 86400, body.client_id, replaces=invitation_id)

    @app.post("/api/invitations/accept")
    def accept_invitation(body: AcceptInvitation, request: Request, response: Response):
        credential = credentials(request, optional=True)
        result = store.accept_invitation(
            body.token.get_secret_value(),
            user_id=credential[0] if credential else None,
            name=body.name,
            password=body.password.get_secret_value() if body.password else None,
        )
        return session_response(result, response, body.transport) if "access_token" in result else result

    @app.get("/api/organizations/{organization_id}/provider-keys")
    def keys(organization_id: str, user: User):
        return store.keys(user, organization_id)

    @app.post("/api/organizations/{organization_id}/provider-keys", status_code=201)
    def add_key(organization_id: str, body: ProviderKey, user: User):
        return store.add_key(
            user,
            organization_id,
            body.provider,
            body.label,
            body.key.get_secret_value(),
        )

    @app.delete("/api/organizations/{organization_id}/provider-keys/{key_id}", status_code=204)
    def disable_key(organization_id: str, key_id: str, user: User):
        store.disable_key(user, organization_id, key_id)

    @app.get("/api/organizations/{organization_id}/policy")
    def policy(organization_id: str, user: User):
        return store.policy(user, organization_id)

    @app.put("/api/organizations/{organization_id}/policy")
    def set_policy(organization_id: str, body: Policy, user: User):
        return store.set_policy(user, organization_id, body.allowed_models, body.monthly_budget_microusd)

    @app.get("/api/organizations/{organization_id}/audit")
    def audit(organization_id: str, user: User, after: Annotated[int, Query(ge=0)] = 0):
        return store.audit(user, organization_id, after)

    if config.frontend_dir is not None:
        app.mount(
            "/assets",
            FrontendAssets(config.frontend_dir / "assets", retained_asset_directories(config.frontend_dir)),
            name="frontend-assets",
        )
        if (config.frontend_dir / "licenses").is_dir():
            app.mount("/licenses", StaticFiles(directory=config.frontend_dir / "licenses"), name="frontend-licenses")

        @app.get("/", include_in_schema=False)
        @app.get("/organization.html", include_in_schema=False)
        def frontend():
            return FileResponse(config.frontend_dir / "organization.html", media_type="text/html")

        @app.get("/reasoning.svg", include_in_schema=False)
        def icon():
            return FileResponse(config.frontend_dir / "reasoning.svg", media_type="image/svg+xml")

    return app
