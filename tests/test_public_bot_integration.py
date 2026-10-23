"""Regression checks for grounded bot context, goals and shared computation.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import json

import pytest
from test_public_bot_runtime import setup
from test_public_bots import fixture as bot_fixture
from test_public_compute import compute as compute
from test_public_compute import finish, submission, worker
from test_public_server import installation as installation

from reasoning_ledger.server.bot_compute import BotCompute
from reasoning_ledger.server.bot_delegation import delegate, tasks
from reasoning_ledger.server.bot_runner import topic_workspace
from reasoning_ledger.server.bot_runtime import BotRuntime
from reasoning_ledger.server.bot_store import BotStore
from reasoning_ledger.server.bot_tools import BotTools
from reasoning_ledger.server.inference_store import InferenceStore
from reasoning_ledger.server.research_goals import ResearchGoals
from reasoning_ledger.server.review_verdict import extract
from reasoning_ledger.server.store import StoreError


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


def test_bot_account_tools_preserve_candidates_and_recheck_live_write_scope(fixture):
    from reasoning_ledger.server.research_accounts import ResearchAccounts

    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    lease = runtime.claim(bot["token"])["lease"]
    tools = BotTools(runtime)
    names = {tool["name"] for tool in tools.context(bot["token"], run["id"], lease)["tools"]}
    assert {"account_read", "account_update"} <= names and "account_request_review" not in names
    arguments = {
        "expected_revision": 0,
        "changes": {
            "question": "An exact sustained question",
            "argument": "A candidate argument.",
            "gaps": "This still needs independent checking.",
        },
    }
    first = tools.invoke(bot["token"], run["id"], lease, "account_update", arguments, "first-account")
    assert first["revision"] == 1 and first["created_by"] == bot["id"]
    assert tools.invoke(bot["token"], run["id"], lease, "account_update", arguments, "first-account")["revision"] == 1
    assert (
        ResearchAccounts(store).read(owner["user_id"], owner["organization_id"], topics[1])["body"]["gaps"] == arguments["changes"]["gaps"]
    )
    runtime.bots.update(
        owner["user_id"],
        owner["organization_id"],
        bot["id"],
        "Codex",
        topics[1:],
        ["messages:read", "messages:write", "knowledge:read"],
        personal=True,
    )
    with pytest.raises(StoreError):
        tools.invoke(
            bot["token"], run["id"], lease, "account_update", {"expected_revision": 1, "changes": {"gaps": "Overwrite"}}, "revoked-write"
        )
    assert (
        tools.invoke(bot["token"], run["id"], lease, "account_read", {"revision": 1, "field": "argument"}, "read-account")["text"]
        == "A candidate argument."
    )


def test_bot_publishes_workspace_tex_to_shared_revision_links_and_rechecks_write_grants(fixture, tmp_path):
    from types import SimpleNamespace

    from reasoning_ledger.agents.tools import ToolDefinition, ToolRegistry
    from reasoning_ledger.server.bot_media import register_bot_media
    from reasoning_ledger.server.research_artifacts import ResearchArtifacts

    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    lease = runtime.claim(bot["token"])["lease"]
    service, registry = BotTools(runtime), ToolRegistry()
    registry.register(
        ToolDefinition(
            "artifact_write",
            "Save project text",
            {"type": "object"},
            lambda **body: service.invoke(bot["token"], run["id"], lease, "artifact_write", body, "publish-text"),
        )
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    source = "\\documentclass{article}\nA precise UTF-8 argument: α.\n"
    (workspace / "argument.tex").write_text(source)
    register_bot_media(SimpleNamespace(workspace=workspace), registry)
    saved = registry.invoke("workspace_publish_text", {"path": "argument.tex"})
    assert saved["revision"] == 1 and saved["url"] in saved["markdown"]
    assert ResearchArtifacts(store).content(owner["user_id"], owner["organization_id"], topics[1], "argument.tex", 1)[0] == source.encode()
    runtime.bots.update(
        owner["user_id"],
        owner["organization_id"],
        bot["id"],
        "Codex",
        topics[1:],
        ["messages:read", "messages:write", "knowledge:read"],
        personal=True,
    )
    with pytest.raises(StoreError):
        registry.invoke("workspace_publish_text", {"path": "argument.tex"})


def test_tmp7_followup_receives_prior_question_and_gateway_tools(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, model, first, _ = setup(fixture)
    claimed = runtime.claim(bot["token"])
    assert claimed["context"][-1]["text"] == "Explain 2=2"
    runtime.finish(bot["token"], first["id"], claimed["lease"], "succeeded", output="Prior answer")
    message = store.post_message(owner["user_id"], owner["organization_id"], topics[1], "What about dimension five?", "followup")
    runtime.enqueue(owner["user_id"], owner["organization_id"], topics[1], model, "second", through=message["sequence"])
    following = runtime.claim(bot["token"])
    assert [item["text"] for item in following["context"]] == ["Explain 2=2", "Prior answer", "What about dimension five?"]
    tools = BotTools(runtime)
    names = {item["name"] for item in tools.context(bot["token"], following["id"], following["lease"])["tools"]}
    assert {"topic_context", "arxiv_read", "web_fetch", "tool_search", "agent_delegate"} <= names
    readiness = tools.invoke(bot["token"], following["id"], following["lease"], "topic_context", {}, "bootstrap")
    assert readiness["ready"] and readiness["context"][0]["text"] == "Explain 2=2"
    assert runtime.get(owner["user_id"], owner["organization_id"], following["id"])["gateway"]["ready"]


def test_workspace_survives_model_changes_without_touching_old_files(tmp_path):
    scope = {key: str(index) * 32 for index, key in enumerate(("organization_id", "project_id", "channel_id", "topic_id"), 1)}
    first = topic_workspace(tmp_path, {**scope, "model": "one"})
    second = topic_workspace(tmp_path, {**scope, "model": "two"})
    assert first == second and first.is_relative_to(tmp_path)
    assert len(first.relative_to(tmp_path).parts) == 4


def test_effort_is_retained_and_unsupported_bot_override_is_refused(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, model, first, _ = setup(fixture)
    run = runtime.claim(bot["token"])
    runtime.finish(bot["token"], first["id"], run["lease"], "succeeded", output="First")
    runtime.advertise(bot["token"], "codex", ["gpt-fixture"], False, False, capabilities={"efforts": {"gpt-fixture": ["high"]}})
    text = f"@[{bot['id']}:gpt-fixture!high] Continue"
    message = store.post_message(owner["user_id"], owner["organization_id"], topics[1], text, "effort-mention")
    admitted = runtime.from_message(bot["token"], message["id"])
    assert admitted["effort"] == "high"
    assert runtime.claim(bot["token"])["effort"] == "high"
    with pytest.raises(StoreError, match="does not advertise"):
        runtime.enqueue(
            owner["user_id"],
            owner["organization_id"],
            topics[1],
            f"bot/{bot['id']}/gpt-fixture",
            "bad-effort",
            through=message["sequence"],
            effort="xhigh",
        )


def test_effort_metadata_uses_actual_model_levels_and_default_is_not_forced(monkeypatch):
    from reasoning_ledger.server import reasoning_effort
    from reasoning_ledger.server.bot_agent import agent_arguments

    monkeypatch.setattr(reasoning_effort, "codex_catalog", lambda: {"example": ["high", "max"], "other": ["high"]})
    assert reasoning_effort.cli_efforts("codex", "example") == ["high", "max"]
    assert reasoning_effort.cli_efforts("codex", "default") == ["high"]
    assert reasoning_effort.cli_efforts("codex", "unknown") == []
    assert reasoning_effort.openrouter_efforts({"reasoning": {"supported_efforts": ["none", "high"], "mandatory": True}}) == ["high"]
    assert reasoning_effort.openrouter_efforts({"supported_parameters": ["reasoning"]}) == []
    codex = agent_arguments("codex", "/workspace", effort="high")
    assert 'model_reasoning_effort="high"' in codex
    assert not any("model_reasoning_effort" in arg for arg in agent_arguments("codex", "/workspace"))
    claude = agent_arguments("claude", "/workspace", effort="high")
    assert claude[claude.index("--effort") + 1] == "high"


def test_bot_recent_context_preserves_admitted_message_revision(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, _, message = setup(fixture)
    store.edit_message(owner["user_id"], owner["organization_id"], topics[1], message["id"], "Later edited question", 0)
    run = runtime.claim(bot["token"])
    assert run["prompt"] == "Explain 2=2"
    assert run["context"][-1]["text"] == "Explain 2=2"


def test_bot_goal_needs_no_openrouter_key_and_continues_until_checkpoint(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, model, first, _ = setup(fixture)
    initial = runtime.claim(bot["token"])
    runtime.finish(bot["token"], first["id"], initial["lease"], "succeeded", output="Initial answer")
    goals = ResearchGoals(InferenceStore(store))
    goal = goals.create(
        owner["user_id"],
        owner["organization_id"],
        topics[1],
        client_id="g" * 16,
        objective="Investigate a question",
        acceptance=["Record the result"],
        model=model,
        budget_microusd=1,
        max_turns=5,
        max_tool_calls=20,
        duration_seconds=3600,
    )

    class NoProvider:
        async def quotes(self, models):
            raise AssertionError("bot goals must not contact OpenRouter")

    asyncio.run(goals.dispatch(NoProvider()))
    run = runtime.claim(bot["token"])
    assert "Continue the active goal" in run["prompt"]
    tools = BotTools(runtime)
    tools.invoke(bot["token"], run["id"], run["lease"], "goal_checkpoint", {"summary": "Progress"}, "cp1")
    runtime.finish(bot["token"], run["id"], run["lease"], "succeeded", output="Progress")
    next_run = runtime.claim(bot["token"])
    assert next_run["id"] != run["id"]
    tools.invoke(
        bot["token"],
        next_run["id"],
        next_run["lease"],
        "goal_checkpoint",
        {"summary": "Acceptance criterion met", "status": "complete"},
        "cp2",
    )
    runtime.finish(bot["token"], next_run["id"], next_run["lease"], "succeeded", output="Done")
    assert runtime.claim(bot["token"]) is None
    assert goals.current(owner["user_id"], owner["organization_id"], topics[1])["status"] == "complete"
    assert goal["billing_source"] == "bot_account"


def test_delegation_is_narrow_and_integrates_once(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, first, _ = setup(fixture)
    parent = runtime.claim(bot["token"])
    task = delegate(runtime, bot["token"], parent["id"], parent["lease"], "librarian", "Find sources", "Only this context")
    assert delegate(runtime, bot["token"], parent["id"], parent["lease"], "librarian", "Find sources", "Only this context") == {
        "id": task["id"],
        "child_run": task["child_run"],
    }
    runtime.finish(bot["token"], first["id"], parent["lease"], "succeeded", output="Delegated")
    child = runtime.claim(bot["token"])
    assert child["context"] == [] and "Only this context" in child["prompt"]
    names = {item["name"] for item in BotTools(runtime).context(bot["token"], child["id"], child["lease"])["tools"]}
    assert not {"topic_history", "messages_search", "topics_search"} & names
    runtime.finish(bot["token"], child["id"], child["lease"], "succeeded", output="Retrieved sources")
    integration = runtime.claim(bot["token"])
    assert "Retrieved sources" in integration["prompt"]
    assert tasks(runtime, bot["token"], integration["id"], integration["lease"], task_id=task["id"])["output_text"] == "Retrieved sources"
    runtime.finish(bot["token"], integration["id"], integration["lease"], "succeeded", output="Integrated")
    assert runtime.claim(bot["token"]) is None


def test_bot_compute_uses_owner_approval_and_exact_existing_image_queue(compute):
    _, store, state, _, owner, topics = compute
    bots = BotStore(store)
    bot = bots.create(
        owner["user_id"],
        owner["organization_id"],
        "Compute bot",
        [topics[1]["id"]],
        ["messages:read", "messages:write", "knowledge:read", "knowledge:write"],
        personal=True,
    )
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], False, False)
    msg = store.post_message(owner["user_id"], owner["organization_id"], topics[1]["id"], "Compute", "compute-message")
    runtime.enqueue(
        owner["user_id"], owner["organization_id"], topics[1]["id"], f"bot/{bot['id']}/default", "compute-run", through=msg["sequence"]
    )
    run = runtime.claim(bot["token"])
    bridge = BotCompute(runtime, state)
    parameters = {key: value for key, value in submission().items() if key != "client_id"}
    approval = bridge.request(bot["token"], run["id"], run["lease"], **parameters)
    assert approval == bridge.request(bot["token"], run["id"], run["lease"], **parameters)
    assert state.jobs(owner["user_id"], owner["organization_id"], topics[1]["id"]) == []
    goal = ResearchGoals(InferenceStore(store)).create(
        owner["user_id"],
        owner["organization_id"],
        topics[1]["id"],
        client_id="compute-goal" * 2,
        objective="Check the computation",
        acceptance=["Inspect the result"],
        model=f"bot/{bot['id']}/default",
        budget_microusd=1,
        max_turns=5,
        max_tool_calls=20,
        duration_seconds=3600,
    )
    with store.transaction(write=True) as db:
        db.execute("UPDATE agent_goals SET bot_run_id=? WHERE id=?", (run["id"], goal["id"]))
    from reasoning_ledger.server.bot_goals import checkpoint

    with pytest.raises(StoreError, match="owner compute approval is pending"):
        checkpoint(runtime, bot["token"], run["id"], run["lease"], "Not actually complete", "complete")
    result = bridge.decide(owner["user_id"], owner["organization_id"], approval["id"], True)
    assert bridge.decide(owner["user_id"], owner["organization_id"], approval["id"], True) == result
    assert result["job_id"] and result["image_digest"] == "sha256:" + "a" * 64
    assert len(state.jobs(owner["user_id"], owner["organization_id"], topics[1]["id"])) == 1
    runtime.finish(bot["token"], run["id"], run["lease"], "succeeded", output="Awaiting computation")
    assert runtime.claim(bot["token"]) is None  # No paid waiting/polling invocation.
    _, client = worker(compute)
    claimed = client.post("/api/worker/claim", json={}).json()
    assert finish(client, claimed).status_code == 200
    integration = runtime.claim(bot["token"])
    assert result["job_id"] in integration["prompt"] and "compute_result" in integration["prompt"]


def test_tmp5_review_verdict_is_independent_of_progress_and_json_format():
    verdict = {"verdict": "inconclusive", "obligations": [{"statement": "Citation", "status": "unclear", "note": "Unavailable"}]}
    for text in [json.dumps(verdict) + "\nReport", "I am checking.\n```json\n" + json.dumps(verdict, indent=2) + "\n```\nReport"]:
        parsed, report = extract(text)
        assert parsed == verdict and "Report" in report
    with pytest.raises(ValueError):
        extract("Verified! No structured verdict")
    with pytest.raises(ValueError):
        extract(json.dumps(verdict) + json.dumps(verdict))
