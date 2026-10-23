"""Separate bot bearer endpoints and human-administrator credential management."""

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from pydantic import Field

from .bot_developer import BotDeveloper
from .bot_pairing import BotPairing
from .bot_runtime import BotRuntime
from .bot_sharing import BotSharing
from .bot_store import BotStore
from .bot_tools import BotTools
from .knowledge_store import KnowledgeStore
from .request_input import RequestInput as Input
from .store import StoreError


class Permissions(Input):
    name: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
    topic_ids: Annotated[list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]], Field(max_length=64)]
    public_access: bool = False
    scopes: Annotated[
        list[Literal["messages:read", "messages:write", "knowledge:read", "knowledge:write"]], Field(min_length=1, max_length=4)
    ]


class Creation(Permissions):
    lifetime_seconds: Annotated[int, Field(ge=3600, le=31_536_000, strict=True)] = 2_592_000


class Rotation(Input):
    lifetime_seconds: Annotated[int, Field(ge=3600, le=31_536_000, strict=True)] = 2_592_000


class Connection(Input):
    device_secret: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
    user_code: Annotated[str, Field(pattern=r"^[A-Z2-9]{12}$")]
    credential_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    name: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
    engine: Literal["codex", "claude"]


class Approval(Input):
    topic_ids: Annotated[list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]], Field(max_length=64)]
    public_access: bool = False
    scopes: Annotated[
        list[Literal["messages:read", "messages:write", "knowledge:read", "knowledge:write"]], Field(min_length=1, max_length=4)
    ]
    lifetime_seconds: Annotated[int, Field(ge=3600, le=31_536_000, strict=True)] = 2_592_000


class Message(Input):
    content: Annotated[str, Field(min_length=1, max_length=16_000, pattern=r"\S")]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]


class KnowledgeWrite(Input):
    client_id: Annotated[str, Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
    kind: Literal["ledger", "bibliography", "journal"]
    title: Annotated[str, Field(min_length=1, max_length=300, pattern=r"\S")]
    body: Annotated[str, Field(min_length=1, max_length=64_000, pattern=r"\S")]
    state: Annotated[str, Field(min_length=1, max_length=20)]
    scope_kind: Literal["organization", "project", "topic"] | None = None
    scope_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    expected_revision: Annotated[int, Field(ge=1, le=9_999, strict=True)] | None = None


class RuntimeAdvertisement(Input):
    engine: Literal["codex", "claude"]
    models: Annotated[list[Annotated[str, Field(max_length=100)]], Field(min_length=1, max_length=20)]
    # The runner's --allow-other-users flag: recorded, but who may invoke the bot is its sharing setting.
    allow_other_users: bool = False
    local_tools: bool = False
    capabilities: dict = Field(default_factory=dict)


class BotInference(Input):
    model: Annotated[str, Field(max_length=150)]
    client_id: Annotated[str, Field(min_length=1, max_length=128)]
    through_sequence: Annotated[int, Field(ge=1, strict=True)]
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class Lease(Input):
    lease: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]


class DiagnosticTarget(Lease):
    topic_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    target_run_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    request_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")] | None = None


class Progress(Lease):
    phase: Literal["thinking", "reading", "writing", "using_tools", "waiting_approval", "finishing"] | None = None
    activity: Annotated[str, Field(max_length=40)] | None = None


class GatewayReceipt(Lease):
    stage: Literal["transport", "cli"]
    tool_count: Annotated[int, Field(ge=1, le=256, strict=True)]


Stage = Annotated[int, Field(ge=0, le=86_400_000, strict=True)]


class RunnerTiming(Input):
    """Content-free runner stages in milliseconds since the runner received the claim."""

    cli_started_ms: Stage | None = None
    cli_ready_ms: Stage | None = None
    first_text_ms: Stage | None = None
    final_text_ms: Stage | None = None
    cli_exited_ms: Stage | None = None
    resolved_model: Annotated[str, Field(max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")] | None = None


class Completion(Lease):
    status: Literal["succeeded", "failed", "outcome_unknown", "cancelled"]
    output: Annotated[str, Field(max_length=128 * 1024)] = ""
    error: Literal["cli_failed", "runner_lost", "cancelled", "gateway_unavailable", "gateway_not_attached"] | None = None
    timing: RunnerTiming | None = None


class ExecutionRequest(Lease):
    command: Annotated[list[Annotated[str, Field(min_length=1, max_length=8000)]], Field(min_length=1, max_length=32)]


class ExecutionDecision(Input):
    allow: bool
    request_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None


class SourceMessage(Input):
    message_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class ToolInvocation(Lease):
    name: Annotated[str, Field(max_length=100)]
    arguments: dict = Field(default_factory=dict)
    client_id: Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]


class KnowledgeQuery(Lease):
    scope: Literal["working_set", "topic", "project"] | None = None
    before: Annotated[str | None, Field(max_length=100)] = None
    kind: Literal["ledger", "bibliography", "journal"] | None = None
    query: Annotated[str, Field(max_length=200)] = ""


class DeveloperPermissions(Input):
    enabled: bool = False
    repositories: Annotated[list[Annotated[str, Field(max_length=140)]], Field(max_length=20)] = Field(default_factory=list)
    requesters: Annotated[list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]], Field(max_length=64)] = Field(default_factory=list)
    issue_self_approvers: Annotated[list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]], Field(max_length=64)] = Field(
        default_factory=list)
    issue_daily_limit: Annotated[int, Field(ge=1, le=20, strict=True)] = 3


class Sharing(Input):
    policy: Literal["owner", "members", "topic_writers"]
    members: Annotated[list[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]], Field(max_length=64)] = Field(
        default_factory=list)


class IssueRequest(Lease):
    repository: Annotated[str, Field(max_length=140)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    body: Annotated[str, Field(min_length=1, max_length=16000)]
    client_id: Annotated[str, Field(min_length=1, max_length=100)]


class IssueReceipt(Lease):
    issue_url: Annotated[str, Field(max_length=300)] | None = None
    unknown: bool = False


def credential(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, access = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not access or len(access) > 128:
        raise StoreError(401, "bot bearer credential required; cookies are not accepted")
    return access


def install_routes(app: FastAPI, state: BotStore, authenticated, *, compute=None, deployment=None) -> None:
    User = Annotated[str, Depends(authenticated)]
    knowledge = KnowledgeStore(state.store)
    pairing = BotPairing(state)
    runtime = BotRuntime(state)
    developer = BotDeveloper(runtime, deployment=deployment)
    sharing = BotSharing(state)
    tools = BotTools(runtime, compute=compute)
    from .bot_compute import BotCompute

    bot_compute = BotCompute(runtime, compute)

    @app.get("/api/organizations/{organization_id}/my-bots/pending-approvals")
    def pending_approvals(organization_id: str, user: User):
        records = [{**row, "kind": kind} for kind, rows in (
            ("execution", runtime.approvals(user, organization_id)),
            ("compute", bot_compute.approvals(user, organization_id)),
            ("issue", developer.approvals(user, organization_id)),
        ) for row in rows if row["status"] == "pending" or row.get("delivery_interrupted")]
        return sorted(records, key=lambda row: (row["created_at"], row["id"]))

    @app.post("/api/bot/runs/{run_id}/receipt")
    def invocation_receipt(run_id: str, request: Request):
        return runtime.receipt(credential(request), run_id)

    @app.get("/api/organizations/{organization_id}/my-bots/compute-approvals")
    def compute_approvals(organization_id: str, user: User):
        return bot_compute.approvals(user, organization_id)

    @app.post("/api/organizations/{organization_id}/my-bots/compute-approvals/{approval_id}")
    def compute_decision(organization_id: str, approval_id: str, body: ExecutionDecision, user: User):
        return bot_compute.decide(user, organization_id, approval_id, body.allow, request_sha256=body.request_sha256)

    @app.put("/api/organizations/{organization_id}/my-bots/{bot_id}/developer")
    def developer_permissions(organization_id: str, bot_id: str, body: DeveloperPermissions, user: User):
        return developer.configure(user, organization_id, bot_id, **body.model_dump())

    @app.put("/api/organizations/{organization_id}/my-bots/{bot_id}/sharing")
    def personal_sharing(organization_id: str, bot_id: str, body: Sharing, user: User):
        return sharing.configure(user, organization_id, bot_id, body.policy, body.members)

    @app.put("/api/organizations/{organization_id}/bots/{bot_id}/sharing")
    def administrator_sharing(organization_id: str, bot_id: str, body: Sharing, user: User):
        return sharing.configure(user, organization_id, bot_id, body.policy, body.members, personal=False)

    @app.get("/api/organizations/{organization_id}/my-bots/issue-approvals")
    def issue_approvals(organization_id: str, user: User):
        return developer.approvals(user, organization_id)

    @app.post("/api/organizations/{organization_id}/my-bots/issue-approvals/{approval_id}")
    def issue_decision(organization_id: str, approval_id: str, body: ExecutionDecision, user: User):
        return developer.approve(user, organization_id, approval_id, body.allow, request_sha256=body.request_sha256)

    @app.post("/api/bot/runs/{run_id}/developer")
    def developer_context(run_id: str, body: Lease, request: Request):
        return developer.context(credential(request), run_id, body.lease)

    @app.post("/api/bot/runs/{run_id}/diagnostics")
    def diagnostics(run_id: str, body: DiagnosticTarget, request: Request):
        value = developer.diagnostics(credential(request), run_id, **body.model_dump())
        value["workers"] = {name: getattr(app.state, name + "_worker").running for name in ("inference", "workflow", "mail", "webhook")}
        value["inference_enabled"] = app.state.config.inference_enabled
        value["mail_enabled"] = app.state.config.mail is not None
        return value

    @app.post("/api/bot/runs/{run_id}/issues")
    def issue_request(run_id: str, body: IssueRequest, request: Request):
        return developer.request(credential(request), run_id, **body.model_dump())

    @app.post("/api/bot/runs/{run_id}/issues/{approval_id}/status")
    def issue_status(run_id: str, approval_id: str, body: Lease, request: Request):
        return developer.receipt(credential(request), run_id, body.lease, approval_id)

    @app.post("/api/bot/runs/{run_id}/issues/{approval_id}/consume")
    def issue_consume(run_id: str, approval_id: str, body: Lease, request: Request):
        return developer.receipt(credential(request), run_id, body.lease, approval_id, consume=True)

    @app.post("/api/bot/runs/{run_id}/issues/{approval_id}/receipt")
    def issue_receipt(run_id: str, approval_id: str, body: IssueReceipt, request: Request):
        return developer.receipt(credential(request), run_id, body.lease, approval_id, **body.model_dump(exclude={"lease"}))

    @app.post("/api/bot/runs/{run_id}/context")
    def run_context(run_id: str, body: Lease, request: Request):
        return tools.context(credential(request), run_id, body.lease)

    @app.post("/api/bot/runs/{run_id}/gateway")
    def gateway_receipt(run_id: str, body: GatewayReceipt, request: Request):
        access = credential(request)
        tools.context(access, run_id, body.lease)
        with state.store.transaction(write=True) as db:
            runtime.leased(db, access, run_id, body.lease)
            import json
            import time

            db.execute("UPDATE bot_runs SET gateway_json=? WHERE id=?", (json.dumps({"ready": True,
                       "stage": body.stage, "tool_count": body.tool_count, "at": int(time.time())}), run_id))
        return {"ready": True}

    @app.post("/api/bot/runs/{run_id}/tools")
    def run_tool(run_id: str, body: ToolInvocation, request: Request):
        return tools.invoke(credential(request), run_id, **body.model_dump())

    @app.post("/api/bot/runs/{run_id}/knowledge")
    def run_knowledge(run_id: str, body: KnowledgeQuery, request: Request):
        return tools.knowledge(credential(request), run_id, **body.model_dump())

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/bot-models")
    def runtime_models(organization_id: str, topic_id: str, user: User):
        return runtime.models(user, organization_id, topic_id)

    @app.get("/api/organizations/{organization_id}/channels/{channel_id}/bot-models")
    def creation_models(organization_id: str, channel_id: str, user: User):
        return runtime.channel_models(user, organization_id, channel_id)

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/bot-runs")
    def bot_runs(organization_id: str, topic_id: str, user: User):
        return runtime.listing(user, organization_id, topic_id)

    @app.get("/api/organizations/{organization_id}/bot-runs/{run_id}")
    def get_bot_run(organization_id: str, run_id: str, user: User):
        return runtime.get(user, organization_id, run_id)

    @app.post("/api/organizations/{organization_id}/bot-runs/{run_id}/stop")
    def stop_bot_run(organization_id: str, run_id: str, user: User):
        return runtime.stop(user, organization_id, run_id)

    @app.post("/api/organizations/{organization_id}/topics/{topic_id}/bot-runs", status_code=201)
    def bot_inference(organization_id: str, topic_id: str, body: BotInference, user: User):
        return runtime.enqueue(user, organization_id, topic_id, body.model, body.client_id,
                               through=body.through_sequence, effort=body.effort, decline=True)

    @app.get("/api/organizations/{organization_id}/my-bots/execution-approvals")
    def execution_approvals(organization_id: str, user: User):
        return runtime.approvals(user, organization_id)

    @app.post("/api/organizations/{organization_id}/my-bots/execution-approvals/{approval_id}")
    def execution_decision(organization_id: str, approval_id: str, body: ExecutionDecision, user: User):
        return runtime.approve(user, organization_id, approval_id, body.allow, request_sha256=body.request_sha256)

    @app.post("/api/bot/runtime")
    def advertise(body: RuntimeAdvertisement, request: Request):
        return runtime.advertise(credential(request), **body.model_dump())

    @app.post("/api/bot/runs/claim")
    def claim_run(request: Request):
        return runtime.claim(credential(request))

    @app.post("/api/bot/runs/from-message", status_code=201)
    def from_message(body: SourceMessage, request: Request):
        return runtime.from_message(credential(request), body.message_id)

    @app.post("/api/bot/runs/{run_id}/progress")
    def run_progress(run_id: str, body: Progress, request: Request):
        return runtime.progress(credential(request), run_id, **body.model_dump())

    @app.post("/api/bot/runs/{run_id}/finish")
    def run_finish(run_id: str, body: Completion, request: Request):
        return runtime.finish(credential(request), run_id, **body.model_dump(exclude_unset=True))

    @app.post("/api/bot/runs/{run_id}/executions")
    def execution_request(run_id: str, body: ExecutionRequest, request: Request):
        return runtime.request_execution(credential(request), run_id, **body.model_dump())

    @app.post("/api/bot/runs/{run_id}/executions/{approval_id}/status")
    def execution_status(run_id: str, approval_id: str, body: Lease, request: Request):
        return runtime.execution(credential(request), run_id, body.lease, approval_id)

    @app.post("/api/bot/runs/{run_id}/executions/{approval_id}/consume")
    def consume_execution(run_id: str, approval_id: str, body: Lease, request: Request):
        return runtime.execution(credential(request), run_id, body.lease, approval_id, consume=True)

    @app.post("/api/bot/connect")
    def connection(body: Connection, request: Request):
        pairing.meter(request.client.host if request.client else "unknown")
        return pairing.start(body.device_secret, body.user_code, body.credential_hash, body.name, body.engine)

    @app.get("/api/bot/connect/status")
    def connection_status(request: Request):
        pairing.meter(request.client.host if request.client else "unknown")
        return pairing.status(credential(request))

    @app.get("/api/bot/connect/{code}")
    def connection_info(code: str, user: User):
        return pairing.info(code)

    @app.post("/api/organizations/{organization_id}/my-bots/connect/{code}")
    def approve_connection(organization_id: str, code: str, body: Approval, user: User):
        return pairing.approve(user, organization_id, code, body.topic_ids, body.scopes, body.lifetime_seconds,
                               public_access=body.public_access)

    @app.get("/api/organizations/{organization_id}/topics/{topic_id}/bots")
    def topic_bots(organization_id: str, topic_id: str, user: User):
        with state.store.transaction() as db:
            state.store._topic_access(db, user, organization_id, topic_id)
            rows = db.execute("SELECT b.* FROM bots b WHERE b.organization_id=?", (organization_id,))
            return [
                {"id": bot["id"], "name": bot["name"], "owner_user_id": bot["created_by"]}
                for row in rows
                if (bot := state._public(db, row))["enabled"] and topic_id in bot["topic_ids"]
            ]

    @app.get("/api/organizations/{organization_id}/my-bots")
    def personal_listing(organization_id: str, user: User):
        return state.list(user, organization_id, personal=True)

    @app.post("/api/organizations/{organization_id}/my-bots", status_code=201)
    def personal_create(organization_id: str, body: Creation, user: User):
        return state.create(user, organization_id, body.name, body.topic_ids, body.scopes, body.lifetime_seconds,
                            personal=True, public_access=body.public_access)

    @app.put("/api/organizations/{organization_id}/my-bots/{bot_id}")
    def personal_update(organization_id: str, bot_id: str, body: Permissions, user: User):
        return state.update(user, organization_id, bot_id, body.name, body.topic_ids, body.scopes,
                            personal=True, public_access=body.public_access)

    @app.post("/api/organizations/{organization_id}/my-bots/{bot_id}/token")
    def personal_rotate(organization_id: str, bot_id: str, body: Rotation, user: User):
        return state.rotate(user, organization_id, bot_id, body.lifetime_seconds, personal=True)

    @app.delete("/api/organizations/{organization_id}/my-bots/{bot_id}", status_code=204)
    def personal_revoke(organization_id: str, bot_id: str, user: User):
        state.revoke(user, organization_id, bot_id, personal=True)
        return Response(status_code=204)

    @app.get("/api/organizations/{organization_id}/bots")
    def listing(organization_id: str, user: User):
        return state.list(user, organization_id)

    @app.post("/api/organizations/{organization_id}/bots", status_code=201)
    def create(organization_id: str, body: Creation, user: User):
        return state.create(user, organization_id, body.name, body.topic_ids, body.scopes, body.lifetime_seconds,
                            public_access=body.public_access)

    @app.put("/api/organizations/{organization_id}/bots/{bot_id}")
    def update(organization_id: str, bot_id: str, body: Permissions, user: User):
        return state.update(user, organization_id, bot_id, body.name, body.topic_ids, body.scopes, public_access=body.public_access)

    @app.post("/api/organizations/{organization_id}/bots/{bot_id}/token")
    def rotate(organization_id: str, bot_id: str, body: Rotation, user: User):
        return state.rotate(user, organization_id, bot_id, body.lifetime_seconds)

    @app.delete("/api/organizations/{organization_id}/bots/{bot_id}", status_code=204)
    def revoke(organization_id: str, bot_id: str, user: User):
        state.revoke(user, organization_id, bot_id)
        return Response(status_code=204)

    @app.get("/api/bot/identity")
    def identity(request: Request):
        return state.identity(credential(request))

    @app.get("/api/bot/topics")
    def topics(request: Request):
        return state.topics(credential(request))

    @app.get("/api/bot/messages")
    def messages(request: Request, after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 100):
        return state.messages(credential(request), after, limit)

    @app.post("/api/bot/topics/{topic_id}/messages", status_code=201)
    def post(topic_id: str, body: Message, request: Request):
        return state.post(credential(request), topic_id, body.content, body.client_id)

    @app.get("/api/bot/knowledge")
    def list_knowledge(
        request: Request,
        kind: Literal["ledger", "bibliography", "journal"] | None = None,
        q: Annotated[str, Query(max_length=200)] = "",
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
        before: Annotated[str | None, Query(max_length=45)] = None,
    ):
        return knowledge.list_bot(state, credential(request), kind=kind, query=q, limit=limit, before=before)

    @app.get("/api/bot/knowledge/{entry_id}")
    def get_knowledge(request: Request, entry_id: str, revision: Annotated[int | None, Query(ge=1, le=10_000)] = None):
        return knowledge.get_bot(state, credential(request), entry_id, revision)

    @app.post("/api/bot/knowledge", status_code=201)
    def create_knowledge(body: KnowledgeWrite, request: Request):
        return knowledge.write_bot(state, credential(request), **body.model_dump(exclude={"expected_revision"}))

    @app.put("/api/bot/knowledge/{entry_id}")
    def revise_knowledge(entry_id: str, body: KnowledgeWrite, request: Request):
        if body.scope_kind is not None or body.scope_id is not None or body.expected_revision is None:
            raise StoreError(400, "revisions require expected_revision without a new scope")
        return knowledge.write_bot(state, credential(request), entry_id=entry_id, **body.model_dump(exclude={"scope_kind", "scope_id"}))
