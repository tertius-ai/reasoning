"""Developer bot authorization, exact consent and unknown outbound effects; no real GitHub writes.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import json
import subprocess
import sys

import httpx
import pytest
from test_public_bot_runtime import setup
from test_public_bots import fixture as bot_fixture
from test_public_bots import member

from reasoning_ledger.agents.tools import ToolRegistry
from reasoning_ledger.deployment.private import read_document
from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.bot_client import BotClientError
from reasoning_ledger.server.bot_developer import BotDeveloper
from reasoning_ledger.server.bot_issue_gateway import github_executable, publish_issue, register
from reasoning_ledger.server.store import StoreError


def test_diagnostics_follow_authorized_topic_and_run_with_live_grants(fixture):
    store, owner, topics, _, _, _ = fixture
    developer, bot, run, lease = enabled(fixture)
    args = (bot["token"], run["id"], lease)
    org = owner["organization_id"]
    target = topics[0]
    # Setup grants only the beta topic. A supplied target does not widen them.
    with pytest.raises(StoreError) as denied:
        developer.diagnostics(*args, topic_id=target)
    assert denied.value.status == 404
    developer.runtime.bots.update(owner["user_id"], org, bot["id"], bot["name"], topics,
                                  ["messages:read", "messages:write", "knowledge:read", "knowledge:write"])
    value = developer.diagnostics(*args, topic_id=target)
    assert value["topic_id"] == target and value["scope"] == "authorized_topic"
    assert value["records"]["workflows"] == [] and value["workflow_capacity"]["limit"] == 8
    value = developer.diagnostics(*args, target_run_id=args[1])
    assert value["records"]["bots"][0]["id"] == args[1]
    assert bot["token"] not in json.dumps(value)
    with pytest.raises(StoreError):
        developer.diagnostics(*args, topic_id=target, target_run_id=args[1])


def test_actual_client_gateway_mcp_composes_developer_discovery_and_preserves_approval(fixture, tmp_path, monkeypatch):
    """Real client allowlist, FastAPI handlers, registry and MCP calls; no publisher effects."""
    from concurrent.futures import ThreadPoolExecutor

    from mcp.shared.memory import create_connected_server_and_client_session

    from reasoning_ledger.agents.tools import build_mcp_bridge
    from reasoning_ledger.deployment.private import write_document
    from reasoning_ledger.server.bot_client import BotClient
    from reasoning_ledger.server.bot_gateway import Gateway

    store, owner, _, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture, local_tools=True)
    developer = BotDeveloper(runtime)
    developer.configure(owner["user_id"], owner["organization_id"], bot["id"], True, ["research/bench", "research/secondary"], [])
    lease = runtime.claim(bot["token"])["lease"]
    app = create_app(Config(store.data_dir, "https://server.example", inference_enabled=False))
    source, private, workspace = (tmp_path / name for name in ("checkout", "private", "workspace"))
    for path in (source, private, workspace):
        path.mkdir(mode=0o700)
    (source / "src/reasoning_ledger/server").mkdir(parents=True)
    (source / "src/reasoning_ledger/server/api.py").write_text("# harmless tracked fixture\n")
    (source / "Makefile").write_text("smoke:\n\tpytest tests/test_public_server.py\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org",
                    "commit", "-qm", "fixture"], check=True)
    policy = private / "developer.json"
    write_document(policy, {"format": 1, "repository": "research/bench", "source_root": str(source), "profiles": ["smoke"]})
    monkeypatch.setattr("reasoning_ledger.server.bot_issue_gateway.github_executable", lambda _: "/usr/bin/fixture-gh")
    events = []
    # Sync BotClient transport forwards unchanged requests to the real ASGI app on a separate loop.
    async def dispatch(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            response = await client.request(request.method, str(request.url), headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def transport(request):
            events.append(request.url.path)
            return pool.submit(lambda: asyncio.run(dispatch(request))).result()
        client = BotClient({"server": "https://server.example", "token": bot["token"]}, transport=httpx.MockTransport(transport))
        gateway = Gateway({"run_id": run["id"], "lease": lease, "workspace": str(workspace), "local_tools": True,
                           "receipt_directory": str(private), "developer_policy": str(policy)}, client=client)
        registry = gateway.registry()
        async def check():
            async with create_connected_server_and_client_session(build_mcp_bridge(registry)) as session:
                async def invoke(name, args):
                    result = await session.call_tool(name, args)
                    assert not result.isError, result.content
                    return json.loads(result.content[0].text)
                names = {tool.name for tool in (await session.list_tools()).tools}
                expected = {"server_diagnostics", "development_source_read", "development_test", "github_issue_create"}
                assert expected <= names
                assert expected <= set((await invoke("topic_context", {}))["tools"])
                found = await invoke("tool_search", {"query": "development_source_read"})
                assert "development_source_read" in {tool["name"] for tool in found}
                assert (await invoke("tool_call", {"name": "server_diagnostics", "arguments": {}}))["result"]["sanitized"]
                read = (await invoke("tool_call", {"name": "development_source_read", "arguments": {"path": "Makefile"}}))["result"]
                assert "smoke:" in read["text"] and len(read["source_revision"]) == 40
                tools = await invoke("tool_search", {"query": "development_test"})
                assert tools[0]["input_schema"]["properties"]["profile"]["enum"] == ["smoke"]
                discovered = await invoke("tool_search", {"query": "github_issue_create"})
                assert discovered[0]["input_schema"]["properties"]["repository"]["enum"] == ["research/bench", "research/secondary"]
                developer.configure(owner["user_id"], owner["organization_id"], bot["id"], True, ["research/bench"], [])
                narrowed = await invoke("tool_search", {"query": "github_issue_create"})
                assert narrowed[0]["input_schema"]["properties"]["repository"]["enum"] == ["research/bench"]
                # Exact issues use actual routing/receipts; consent remains human-controlled and one-shot.
                approval = gateway.request("/issues", repository="research/bench", title="Exact fixture",
                                           body="No publication", client_id="fixture")
                assert gateway.request(f"/issues/{approval['id']}/status")["status"] == "pending"
                with pytest.raises(BotClientError, match="HTTP 409"):
                    gateway.request(f"/issues/{approval['id']}/consume")
                developer.approve(owner["user_id"], owner["organization_id"], approval["id"], True)
                assert gateway.request(f"/issues/{approval['id']}/consume")["status"] == "consumed"
                with pytest.raises(BotClientError, match="HTTP 409"):
                    gateway.request(f"/issues/{approval['id']}/consume")
                assert gateway.request(f"/issues/{approval['id']}/receipt", unknown=True)["status"] == "outcome_unknown"
                # Execute approval endpoints are already allowed; exercise exact frozen profile consumption too.
                command = ["reasoning-dev-test", "research/bench", "smoke", "b" * 40, "c" * 64]
                approval = gateway.request("/executions", command=command)
                assert gateway.request(f"/executions/{approval['id']}/status")["status"] == "pending"
                runtime.approve(owner["user_id"], owner["organization_id"], approval["id"], True)
                assert gateway.request(f"/executions/{approval['id']}/consume")["status"] == "consumed"
                original = BotDeveloper.request
                published = []
                def approved_request(self, *args, **values):
                    preview = original(self, *args, **values)
                    if preview["status"] == "pending":
                        self.approve(owner["user_id"], owner["organization_id"], preview["id"], True)
                    return self.receipt(args[0], args[1], values["lease"], preview["id"])
                def publish(repository, title, body, **options):
                    receipts = list(private.glob("github-issue-*.json"))
                    assert len(receipts) == 1 and read_document(receipts[0])["status"] == "outcome_unknown"
                    published.append((repository, title, body))
                    return "https://github.com/research/bench/issues/7"
                monkeypatch.setattr(BotDeveloper, "request", approved_request)
                monkeypatch.setattr("reasoning_ledger.server.bot_issue_gateway.publish_issue", publish)
                arguments = {"repository": "research/bench", "title": "Approved fixture",
                             "body": "No real GitHub write", "client_id": "mcp-issue"}
                assert (await invoke("github_issue_create", arguments))["issue_url"].endswith("/7")
                repeated = await invoke("tool_call", {"name": "github_issue_create", "arguments": arguments})
                assert repeated["result"]["issue_url"].endswith("/7")
                assert published == [("research/bench", "Approved fixture", "No real GitHub write")]
                developer.configure(owner["user_id"], owner["organization_id"], bot["id"], True, ["other/repo"], [])
                current_tools = (await invoke("topic_context", {}))["tools"]
                assert "development_source_read" not in current_tools and "github_issue_create" not in current_tools
                assert await invoke("tool_search", {"query": "github_issue_create"}) == []
                denied_issue = await session.call_tool("tool_call", {"name": "github_issue_create", "arguments": arguments})
                assert denied_issue.isError and len(published) == 1
                denied = await session.call_tool("tool_call", {"name": "development_source_read", "arguments": {"path": "Makefile"}})
                assert denied.isError
                developer.configure(owner["user_id"], owner["organization_id"], bot["id"], False, [], [])
                assert not expected.intersection((await invoke("topic_context", {}))["tools"])
                assert await invoke("tool_search", {"query": "server_diagnostics"}) == []
                assert (await session.call_tool("server_diagnostics", {})).isError
                assert (await session.call_tool("tool_call", {"name": "tool_search", "arguments": {"query": "anything"}})).isError
        try:
            asyncio.run(check())
        finally:
            client.close()
    assert any(path.endswith("/developer") for path in events)
    assert any(path.endswith("/diagnostics") for path in events)


def test_bot_management_and_presence_report_safe_engine_models_and_developer_marker(fixture):
    store, owner, _, _, _, bots = fixture
    runtime, bot, _, _, _ = setup(fixture, local_tools=True)
    BotDeveloper(runtime).configure(owner["user_id"], owner["organization_id"], bot["id"], True, ["research/bench"], [])
    record = next(item for item in bots.list(owner["user_id"], owner["organization_id"], personal=True) if item["id"] == bot["id"])
    assert record["runtime"] == {"engine": "codex", "models": ["default", "test-model"], "local_tools": True, "online": True}
    assert "runtime" not in bots.identity(bot["token"])  # Stable machine identity does not grow management fields.
    presence = next(item for item in store.presence(owner["user_id"], owner["organization_id"])["bots"] if item["id"] == bot["id"])
    assert presence["developer"] and presence["engine"] == "codex" and presence["models"] == ["default", "test-model"]
    assert "repositories" not in presence and bot["token"] not in json.dumps(presence)


@pytest.mark.parametrize("suffix", ["/issues/bad/status", "/issues/" + "a" * 32 + "/delete", "/diagnostics?secret=x", "/../identity"])
def test_client_rejects_noncanonical_developer_routes_before_transport(suffix):
    from reasoning_ledger.server.bot_client import BotClient

    calls = []
    client = BotClient({"server": "https://server.example", "token": "rnb_" + "a" * 43},
                       transport=httpx.MockTransport(lambda request: calls.append(request)))
    try:
        with pytest.raises(BotClientError, match="unsupported"):
            client._request("POST", "/api/bot/runs/" + "b" * 32 + suffix, {})
    finally:
        client.close()
    assert calls == []


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


def enabled(fixture, *, other_users=False):
    runtime, bot, _, run, _ = setup(fixture, other_users=other_users)
    developer = BotDeveloper(runtime)
    developer.configure(bot["created_by"], bot["organization_id"], bot["id"], True, ["research/bench"], [])
    claim = runtime.claim(bot["token"])
    return developer, bot, run, claim["lease"]


def test_developer_disabled_default_and_exact_owner_repo_requester_policy(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, model, run, _ = setup(fixture, other_users=True)
    developer = BotDeveloper(runtime)
    claim = runtime.claim(bot["token"])
    args = (bot["token"], run["id"], claim["lease"])
    assert developer.context(*args) == {"enabled": False}
    with pytest.raises(StoreError):
        developer.diagnostics(*args)
    other = member(fixture)
    with pytest.raises(StoreError):
        developer.configure(other, owner["organization_id"], bot["id"], True, ["research/bench"], [])
    for repo in ["https://github.com/research/bench", "research/../secrets", "-flag/bench"]:
        with pytest.raises(StoreError):
            developer.configure(owner["user_id"], owner["organization_id"], bot["id"], True, [repo], [])
    developer.configure(owner["user_id"], owner["organization_id"], bot["id"], True, ["research/bench"], [])
    assert developer.diagnostics(*args)["sanitized"]
    assert bot["token"] not in json.dumps(developer.diagnostics(*args))
    with pytest.raises(StoreError, match="allowlist"):
        developer.request(*args, "elsewhere/bench", "Title", "Body", "issue")
    runtime.finish(*args, "succeeded", "Finished")
    requested = runtime.enqueue(other, owner["organization_id"], topics[1], model, "other-dev", prompt="file an issue")
    claim = runtime.claim(bot["token"])
    with pytest.raises(StoreError, match="requester"):
        developer.request(bot["token"], requested["id"], claim["lease"], "research/bench", "Title", "Body", "issue")


def test_topic_context_tells_the_model_when_this_requester_lacks_developer_tools(fixture):
    from reasoning_ledger.server.bot_tools import DEVELOPER_NOT_PERMITTED, BotTools

    store, owner, topics, _, _, _ = fixture
    runtime, bot, model, run, _ = setup(fixture, other_users=True)
    developer, tools = BotDeveloper(runtime), BotTools(runtime)
    other, org = member(fixture), owner["organization_id"]
    claim = runtime.claim(bot["token"])
    assert "notices" not in tools.invoke(bot["token"], run["id"], claim["lease"], "topic_context", {}, "owner-context")
    runtime.finish(bot["token"], run["id"], claim["lease"], "succeeded", "Finished")
    requested = runtime.enqueue(other, org, topics[1], model, "other-dev", prompt="file an issue")
    claim = runtime.claim(bot["token"])
    args = (bot["token"], requested["id"], claim["lease"])
    # A bot without developer tools says nothing about them.
    assert "notices" not in tools.invoke(*args, "topic_context", {}, "plain")
    developer.configure(owner["user_id"], org, bot["id"], True, ["research/bench"], [])
    context = tools.invoke(*args, "topic_context", {}, "excluded")
    assert context["notices"] == [DEVELOPER_NOT_PERMITTED] and "research/bench" not in json.dumps(context)
    assert tools.context(*args)["developer"] == {"enabled": False, "reason": "requester_not_permitted"}
    developer.configure(owner["user_id"], org, bot["id"], True, ["research/bench"], [other])
    assert "notices" not in tools.invoke(*args, "topic_context", {}, "permitted")
    assert tools.context(*args)["developer"] == {"enabled": True, "repositories": ["research/bench"]}


def trusted(fixture, *, self_approve=True, limit=3):
    """A developer bot whose owner permits another member, optionally to approve their own issues."""
    store, owner, topics, _, _, _ = fixture
    runtime, bot, model, run, _ = setup(fixture, other_users=True)
    developer, org = BotDeveloper(runtime), owner["organization_id"]
    other = member(fixture)
    developer.configure(owner["user_id"], org, bot["id"], True, ["research/bench"], [other],
                        [other] if self_approve else [], limit)
    runtime.finish(bot["token"], run["id"], runtime.claim(bot["token"])["lease"], "succeeded", "Finished")
    requested = runtime.enqueue(other, org, topics[1], model, "trusted-run", prompt="file an issue")
    lease = runtime.claim(bot["token"])["lease"]
    return developer, bot, other, requested, (bot["token"], requested["id"], lease)


def pending(developer, user, org):
    return [row for row in developer.approvals(user, org) if row["status"] == "pending"]


def test_trusted_requester_approves_own_exact_issue_with_attribution_and_audit(fixture):
    store, owner, topics, _, _, _ = fixture
    developer, bot, other, run, args = trusted(fixture)
    org, owner_id = owner["organization_id"], owner["user_id"]
    approval = developer.request(*args, "research/bench", "Exact title", "Exact body", "trusted")
    attribution = "\n\n---\n\nRequested in Reasoning by `Member` through the bot `Codex`."
    assert approval["approver_id"] == other and approval["body"] == "Exact body" + attribution
    # A retry with the bot's original inputs is the same request; changed inputs are refused.
    assert developer.request(*args, "research/bench", "Exact title", "Exact body", "trusted")["id"] == approval["id"]
    with pytest.raises(StoreError, match="different inputs"):
        developer.request(*args, "research/bench", "Exact title", "Other body", "trusted")
    assert pending(developer, owner_id, org) == []
    [row] = pending(developer, other, org)
    assert row["id"] == approval["id"] and row["approval_role"] == "requester" and row["body"].endswith(attribution)
    # Runs created in the same second have no defined order, so select the requested run by id.
    [listed] = [item for item in developer.runtime.listing(other, org, topics[1]) if item["id"] == run["id"]]
    assert listed["approval_by"] == "requester"
    with pytest.raises(StoreError):
        developer.approve(owner_id, org, approval["id"], True)  # The owner delegated this decision.
    with pytest.raises(StoreError, match="changed"):
        developer.approve(other, org, approval["id"], True, request_sha256="0" * 64)
    assert developer.approve(other, org, approval["id"], True, request_sha256=approval["document_sha256"])["status"] == "approved"
    receipt = developer.receipt(*args, approval["id"], consume=True)
    assert receipt["status"] == "consumed" and receipt["body"].endswith(attribution) and receipt["decided_by"] == other
    url = "https://github.com/research/bench/issues/3"
    assert developer.receipt(*args, approval["id"], issue_url=url)["status"] == "created"
    owner_view = developer.approvals(owner_id, org)
    assert [(item["id"], item["decided_by_name"], item["approval_role"]) for item in owner_view] == [
        (approval["id"], "Member", "requester")]
    with store.transaction() as db:
        audit = db.execute("SELECT actor_id,action FROM audit_events WHERE target_id=?", (approval["id"],)).fetchall()
    assert [tuple(item) for item in audit] == [(other, "bot.issue.approve")]


def test_owner_requests_are_unattributed_and_untrusted_requesters_still_need_the_owner(fixture):
    store, owner, _, _, _, _ = fixture
    developer, bot, other, _, args = trusted(fixture, self_approve=False)
    org = owner["organization_id"]
    approval = developer.request(*args, "research/bench", "Title", "Body", "untrusted")
    assert approval["approver_id"] == owner["user_id"] and approval["body"].startswith("Body\n\n---\n\nRequested in Reasoning by")
    assert pending(developer, other, org) == []
    with pytest.raises(StoreError):
        developer.approve(other, org, approval["id"], True)
    assert pending(developer, owner["user_id"], org)[0]["approval_role"] == "owner"
    runtime, (_, run_id, _) = developer.runtime, args
    runtime.stop(owner["user_id"], org, run_id)
    own_run = runtime.enqueue(owner["user_id"], org, fixture[2][1], f"bot/{bot['id']}/default", "own-run", prompt="file")
    lease = runtime.claim(bot["token"])["lease"]
    own = developer.request(bot["token"], own_run["id"], lease, "research/bench", "Title", "Body", "own")
    assert own["body"] == "Body" and own["approver_id"] == owner["user_id"]


def test_self_approval_daily_limit_routes_excess_requests_to_the_owner(fixture):
    store, owner, _, _, _, _ = fixture
    developer, bot, other, _, args = trusted(fixture, limit=1)
    org = owner["organization_id"]
    first = developer.request(*args, "research/bench", "First", "Body", "first")
    assert first["approver_id"] == other
    developer.approve(other, org, first["id"], True)
    developer.receipt(*args, first["id"], consume=True)
    second = developer.request(*args, "research/bench", "Second", "Body", "second")
    assert second["approver_id"] == owner["user_id"] and pending(developer, other, org) == []
    # Even a request routed to the requester cannot be approved past the limit.
    with store.transaction(write=True) as db:
        db.execute("UPDATE bot_issue_approvals SET approver_id=? WHERE id=?", (other, second["id"]))
    with pytest.raises(StoreError) as limited:
        developer.approve(other, org, second["id"], True)
    assert limited.value.status == 429 and "limit of 1" in limited.value.message
    assert developer.approve(other, org, second["id"], False)["status"] == "denied"


def test_permission_changes_invalidate_self_approval_and_recheck_on_consumption(fixture):
    store, owner, _, _, _, _ = fixture
    developer, bot, other, _, args = trusted(fixture)
    org, owner_id = owner["organization_id"], owner["user_id"]
    approval = developer.request(*args, "research/bench", "Title", "Body", "invalidated")
    developer.configure(owner_id, org, bot["id"], True, ["research/bench"], [other], [], 3)
    with store.transaction() as db:
        assert db.execute("SELECT status FROM bot_issue_approvals WHERE id=?", (approval["id"],)).fetchone()[0] == "denied"
    with pytest.raises(StoreError):
        developer.approve(other, org, approval["id"], True)
    developer.configure(owner_id, org, bot["id"], True, ["research/bench"], [other], [other], 3)
    approval = developer.request(*args, "research/bench", "Title", "Body", "approved-then-revoked")
    developer.approve(other, org, approval["id"], True)
    # A grant edited outside configure() still fences an approved, unconsumed self-approval.
    with store.transaction(write=True) as db:
        db.execute("UPDATE bot_developer_permissions SET issue_self_approvers_json='[]' WHERE bot_id=?", (bot["id"],))
    with pytest.raises(StoreError, match="no longer lets"):
        developer.receipt(*args, approval["id"], consume=True)
    for requesters, invalid in (([other], ["f" * 32]), ([other], [owner_id]), ([other, owner_id], [owner_id])):
        with pytest.raises(StoreError, match="their own issues|already approves"):
            developer.configure(owner_id, org, bot["id"], True, ["research/bench"], requesters, invalid, 3)
    with pytest.raises(StoreError):
        developer.configure(owner_id, org, bot["id"], True, ["research/bench"], [other], [other], 21)


def test_gateway_publishes_the_attributed_preview_approved_by_the_requester(fixture, tmp_path, monkeypatch):
    developer, bot, other, _, args = trusted(fixture)
    org, published = bot["organization_id"], []

    class Gateway:
        workspace = tmp_path
        def request(self, suffix, **values):
            if suffix == "/issues":
                result = developer.request(*args, **values)
                if result["status"] == "pending":
                    developer.approve(other, org, result["id"], True, request_sha256=result["document_sha256"])
                return developer.receipt(*args, result["id"])
            approval_id = suffix.split("/")[2]
            return developer.receipt(*args, approval_id, consume=suffix.endswith("/consume"), **values)
    def publish(repository, title, body, **options):
        published.append(body)
        return "https://github.com/research/bench/issues/8"
    monkeypatch.setattr("reasoning_ledger.server.bot_issue_gateway.publish_issue", publish)
    monkeypatch.setattr("reasoning_ledger.server.bot_issue_gateway.github_executable", lambda _: "/usr/bin/gh")
    registry = ToolRegistry()
    register(Gateway(), registry, tmp_path, {"enabled": True, "repositories": ["research/bench"]})
    arguments = {"repository": "research/bench", "title": "Exact", "body": "Exact body", "client_id": "requester"}
    assert registry.invoke("github_issue_create", arguments)["issue_url"].endswith("/8")
    assert published == ["Exact body\n\n---\n\nRequested in Reasoning by `Member` through the bot `Codex`."]


def test_attribution_names_cannot_mention_link_or_format():
    from reasoning_ledger.server.bot_developer import attribution_label

    assert attribution_label("@team `x` [link](https://e.example)\n**bold**") == "`@team x [link](https://e.example) **bold**`"
    assert attribution_label("") == "`unnamed`" and len(attribution_label("n" * 500)) == 102


def test_exact_issue_approval_consume_once_and_result_receipt(fixture):
    developer, bot, run, lease = enabled(fixture)
    owner, org = bot["created_by"], bot["organization_id"]
    args = (bot["token"], run["id"], lease)
    approval = developer.request(*args, "research/bench", "Exact title", "Exact body", "once")
    assert developer.request(*args, "research/bench", "Exact title", "Exact body", "once")["id"] == approval["id"]
    with pytest.raises(StoreError):
        developer.request(*args, "research/bench", "Changed title", "Exact body", "once")
    with pytest.raises(StoreError):
        developer.receipt(*args, approval["id"], consume=True)
    assert developer.approvals(owner, org)[0]["body"] == "Exact body"
    developer.approve(owner, org, approval["id"], True)
    receipt = developer.receipt(*args, approval["id"], consume=True)
    assert receipt["status"] == "consumed" and receipt["title"] == "Exact title"
    with pytest.raises(StoreError):
        developer.receipt(*args, approval["id"], consume=True)
    with pytest.raises(StoreError):
        developer.receipt(*args, approval["id"], issue_url="https://github.com/elsewhere/bench/issues/1")
    assert developer.receipt(*args, approval["id"], unknown=True)["status"] == "outcome_unknown"
    url = "https://github.com/research/bench/issues/1"
    assert developer.receipt(*args, approval["id"], issue_url=url)["status"] == "created"
    assert developer.receipt(*args, approval["id"], issue_url=url)["issue_url"] == url
    developer.runtime.stop(owner, org, run["id"])
    with pytest.raises(StoreError):
        developer.request(*args, "research/bench", "New", "Body", "new")


def test_development_test_execution_rechecks_server_repository_grant_on_consumption(fixture):
    runtime, bot, _, run, _ = setup(fixture, local_tools=True)
    developer = BotDeveloper(runtime)
    claim = runtime.claim(bot["token"])
    args = (bot["token"], run["id"], claim["lease"])
    command = ["reasoning-dev-test", "research/bench", "smoke", "b" * 40, "c" * 64]
    with pytest.raises(StoreError, match="disabled"):
        runtime.request_execution(*args, command)
    developer.configure(bot["created_by"], bot["organization_id"], bot["id"], True, ["research/bench"], [])
    with pytest.raises(StoreError, match="permitted"):
        runtime.request_execution(*args, [*command[:2], "arbitrary-shell", *command[3:]])
    approval = runtime.request_execution(*args, command)
    runtime.approve(bot["created_by"], bot["organization_id"], approval["id"], True)
    developer.configure(bot["created_by"], bot["organization_id"], bot["id"], False, [], [])
    with pytest.raises(StoreError, match="disabled"):
        runtime.execution(*args, approval["id"], consume=True)
    assert runtime.execution(*args, approval["id"])["status"] == "denied"
    developer.configure(bot["created_by"], bot["organization_id"], bot["id"], True, ["research/bench"], [])
    with pytest.raises(StoreError, match="not been approved"):
        runtime.execution(*args, approval["id"], consume=True)
    approval = runtime.request_execution(*args, [*command[:4], "d" * 64])
    runtime.approve(bot["created_by"], bot["organization_id"], approval["id"], True)
    assert runtime.execution(*args, approval["id"], consume=True)["status"] == "consumed"
    with pytest.raises(StoreError, match="already consumed"):
        runtime.execution(*args, approval["id"], consume=True)


def test_private_issue_preview_disappears_after_owner_loses_access(fixture):
    store, owner, topics, _, _, bots = fixture
    user, org = member(fixture), owner["organization_id"]
    private = store.create_entity(owner["user_id"], org, "projects", "Private", visibility="private", owner_id=owner["user_id"])
    channel = store.create_entity(owner["user_id"], org, "channels", "Private", private["id"])
    topic = store.create_entity(owner["user_id"], org, "topics", "Private", channel["id"], "beta")
    store.change_project_member(owner["user_id"], org, private["id"], user, "member")
    bot = bots.create(user, org, "Dev", [], ["messages:read", "messages:write"], personal=True, public_access=True)
    from reasoning_ledger.server.bot_runtime import BotRuntime

    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], False, False)
    run = runtime.enqueue(user, org, topic["id"], f"bot/{bot['id']}/default", "private", prompt="Diagnose")
    claim = runtime.claim(bot["token"])
    developer = BotDeveloper(runtime)
    developer.configure(user, org, bot["id"], True, ["research/bench"], [])
    developer.request(bot["token"], run["id"], claim["lease"], "research/bench", "Private title", "PRIVATE SECRET", "issue")
    assert developer.approvals(user, org)[0]["body"] == "PRIVATE SECRET"
    store.change_project_member(owner["user_id"], org, private["id"], user, None)
    assert developer.approvals(user, org) == []


@pytest.mark.parametrize("fails", [False, True])
def test_gateway_freezes_receipt_before_effect_and_never_republishes_unknown(fixture, tmp_path, monkeypatch, fails):
    developer, bot, run, lease = enabled(fixture)
    args, calls = (bot["token"], run["id"], lease), []
    class Gateway:
        workspace = tmp_path
        def request(self, suffix, **values):
            if suffix == "/issues":
                result = developer.request(*args, **values)
                if result["status"] == "pending":
                    developer.approve(bot["created_by"], bot["organization_id"], result["id"], True)
                return developer.receipt(*args, result["id"])
            approval_id = suffix.split("/")[2]
            return developer.receipt(*args, approval_id, consume=suffix.endswith("/consume"), **values)
    def publish(repository, title, body, **options):
        receipts = list(tmp_path.glob("github-issue-*.json"))
        assert len(receipts) == 1 and read_document(receipts[0])["status"] == "outcome_unknown"
        calls.append((repository, title, body))
        if fails:
            raise subprocess.TimeoutExpired("fixture", 60)
        return "https://github.com/research/bench/issues/7"
    monkeypatch.setattr("reasoning_ledger.server.bot_issue_gateway.publish_issue", publish)
    monkeypatch.setattr("reasoning_ledger.server.bot_issue_gateway.github_executable", lambda _: "/usr/bin/gh")
    registry = ToolRegistry()
    register(Gateway(), registry, tmp_path, {"enabled": True, "repositories": ["research/bench"]})
    arguments = {"repository": "research/bench", "title": "Exact", "body": "Exact body", "client_id": "once"}
    if fails:
        with pytest.raises(BotClientError, match="unknown"):
            registry.invoke("github_issue_create", arguments)
        with pytest.raises(BotClientError, match="unknown"):
            registry.invoke("github_issue_create", arguments)
    else:
        assert registry.invoke("github_issue_create", arguments)["issue_url"].endswith("/7")
        assert registry.invoke("github_issue_create", arguments)["issue_url"].endswith("/7")
    assert calls == [("research/bench", "Exact", "Exact body")]


def test_github_publisher_uses_fixed_host_stdin_and_never_shell(monkeypatch):
    def run(arguments, **options):
        assert arguments == ["fixture-gh", "api", "--hostname", "github.com", "--method", "POST",
                             "repos/research/bench/issues", "--input", "-", "--jq", ".html_url"]
        assert "shell" not in options and json.loads(options["input"])["title"] == "$(not a shell)"
        return subprocess.CompletedProcess(arguments, 0, "https://github.com/research/bench/issues/9\n", "secret ignored")
    monkeypatch.setattr("subprocess.run", run)
    assert publish_issue("research/bench", "$(not a shell)", "Exact body", executable="fixture-gh").endswith("/9")


def test_github_executable_cannot_come_from_bot_workspace_path_or_symlink(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate = workspace / "gh"
    candidate.write_text("never executed")
    monkeypatch.setattr("shutil.which", lambda _: str(candidate))
    with pytest.raises(BotClientError, match="outside"):
        github_executable(workspace)
    alias = tmp_path / "gh"
    alias.symlink_to(candidate)
    monkeypatch.setattr("shutil.which", lambda _: str(alias))
    with pytest.raises(BotClientError, match="outside"):
        github_executable(workspace)
    executable = tmp_path / "trusted-gh"
    executable.write_text("trusted installation")
    monkeypatch.setattr("shutil.which", lambda _: str(executable))
    assert github_executable(workspace) == str(executable)


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_gateway_python_isolated_from_bot_workspace_imports(tmp_path, engine):
    from reasoning_ledger.server.bot_agent import agent_arguments

    marker = tmp_path / "executed"
    for filename in ["reasoning_ledger.py", "subprocess.py", "httpx.py"]:
        (tmp_path / filename).write_text(f"from pathlib import Path; Path({str(marker)!r}).write_text('unsafe import')")
    arguments = agent_arguments(engine, tmp_path, gateway=tmp_path / "gateway.json")
    if engine == "claude":
        gateway = json.loads(arguments[arguments.index("--mcp-config") + 1])["mcpServers"]["reasoning"]
        assert gateway["args"][:2] == ["-I", "-m"]
    else:
        assert '["-I", "-m", "reasoning_ledger.server.bot_gateway"' in " ".join(arguments)
    result = subprocess.run([sys.executable, "-I", "-c", "import reasoning_ledger.server.bot_gateway; import httpx; import subprocess"],
                            cwd=tmp_path, capture_output=True, check=False)
    assert result.returncode == 0 and not marker.exists()


def test_pending_issue_is_fenced_on_stop_or_developer_permission_change(fixture):
    developer, bot, run, lease = enabled(fixture)
    args = (bot["token"], run["id"], lease)
    approval = developer.request(*args, "research/bench", "Title", "Body", "fenced")
    developer.approve(bot["created_by"], bot["organization_id"], approval["id"], True)
    developer.configure(bot["created_by"], bot["organization_id"], bot["id"], False, [], [])
    with pytest.raises(StoreError):
        developer.receipt(*args, approval["id"], consume=True)
    with developer.store.transaction() as db:
        assert db.execute("SELECT status FROM bot_issue_approvals WHERE id=?", (approval["id"],)).fetchone()[0] == "denied"


def test_developer_wire_requires_human_csrf_owner_approval_and_live_bot_lease(fixture, monkeypatch):
    import fastapi.dependencies.utils
    import fastapi.routing

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(fastapi.dependencies.utils, "run_in_threadpool", inline)
    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", inline)
    store, owner, _, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    lease = runtime.claim(bot["token"])["lease"]
    app = create_app(Config(store.data_dir, "https://server.example", inference_enabled=False))
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://server.example") as client:
            root = f"/api/organizations/{owner['organization_id']}/my-bots"
            client.cookies.set("__Host-reasoning_session", owner["access_token"])
            permissions = {"enabled": True, "repositories": ["research/bench"], "requesters": []}
            assert (await client.put(root + "/" + bot["id"] + "/developer", json=permissions)).status_code == 403
            human = {"origin": "https://server.example", "x-csrf-token": owner["csrf_token"]}
            assert (await client.put(root + "/" + bot["id"] + "/developer", json=permissions, headers=human)).status_code == 200
            for invalid, status in (({"issue_self_approvers": ["f" * 32]}, 400), ({"issue_daily_limit": 21}, 422)):
                response = await client.put(root + "/" + bot["id"] + "/developer", json={**permissions, **invalid}, headers=human)
                assert response.status_code == status
            developer = next(item for item in (await client.get(root)).json() if item["id"] == bot["id"])["developer"]
            assert developer["issue_self_approvers"] == [] and developer["issue_daily_limit"] == 3
            client.cookies.clear()
            bearer = {"authorization": "Bearer " + bot["token"]}
            path = "/api/bot/runs/" + run["id"]
            assert (await client.post(path + "/diagnostics", json={"lease": lease}, headers=bearer)).json()["sanitized"]
            request = {"lease": lease, "repository": "research/bench", "title": "Exact title", "body": "Exact preview", "client_id": "wire"}
            response = await client.post(path + "/issues", json=request, headers=bearer)
            assert response.status_code == 200
            approval = response.json()["id"]
            assert (await client.post(path + "/issues/" + approval + "/consume", json={"lease": lease}, headers=bearer)).status_code == 409
            assert (await client.post(root + "/issue-approvals/" + approval, json={"allow": True}, headers=bearer)).status_code == 401
            client.cookies.set("__Host-reasoning_session", owner["access_token"])
            assert (await client.get(root + "/issue-approvals")).json()[0]["body"] == "Exact preview"
            assert (await client.post(root + "/issue-approvals/" + approval, json={"allow": True})).status_code == 403
            assert (await client.post(root + "/issue-approvals/" + approval, json={"allow": True}, headers=human)).status_code == 200
            client.cookies.clear()
            consumed = await client.post(path + "/issues/" + approval + "/consume", json={"lease": lease}, headers=bearer)
            assert consumed.json()["status"] == "consumed"
            assert (await client.post(path + "/issues/" + approval + "/consume", json={"lease": lease}, headers=bearer)).status_code == 409
    asyncio.run(check())
