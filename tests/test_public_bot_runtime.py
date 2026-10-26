"""Local-agent receipts and execution permission tests; no paid inference.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import asyncio

import httpx
import pytest
from test_public_bots import fixture as bot_fixture
from test_public_bots import member, share

from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.bot_runtime import BotRuntime
from reasoning_ledger.server.store import StoreError


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


def setup(fixture, *, other_users=False, local_tools=False):
    store, owner, topics, _, _, bots = fixture
    bot = bots.create(owner["user_id"], owner["organization_id"], "Codex", topics[1:],
                      ["messages:read", "messages:write", "knowledge:read", "knowledge:write"], personal=True)
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default", "test-model"], other_users, local_tools)
    if other_users:
        share(fixture, bot["id"])
    message = store.post_message(owner["user_id"], owner["organization_id"], topics[1], "Explain 2=2", "request")
    model = f"bot/{bot['id']}/default"
    run = runtime.enqueue(owner["user_id"], owner["organization_id"], topics[1], model, "inference", through=message["sequence"])
    return runtime, bot, model, run, message


def test_idempotent_admission_claim_and_completion(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, model, run, message = setup(fixture)
    assert all(choice["local_tools"] is False for choice in runtime.models(owner["user_id"], owner["organization_id"], topics[1]))
    assert runtime.enqueue(owner["user_id"], owner["organization_id"], topics[1], model, "inference",
                           through=message["sequence"])["id"] == run["id"]
    with pytest.raises(StoreError, match="different inputs"):
        runtime.enqueue(owner["user_id"], owner["organization_id"], topics[1], model, "inference", prompt="Other")
    claimed = runtime.claim(bot["token"])
    assert claimed["id"] == run["id"] and "lease" not in runtime.listing(owner["user_id"], owner["organization_id"], topics[1])[0]
    assert runtime.claim(bot["token"]) is None
    runtime.progress(bot["token"], run["id"], claimed["lease"], "thinking")
    args = (bot["token"], run["id"], claimed["lease"], "succeeded", "By reflexivity.")
    assert runtime.finish(*args) == runtime.finish(*args)
    assert len(store.messages(owner["user_id"], owner["organization_id"], topics[1])) == 2
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 0


def test_explicit_mention_model_and_edit_dedup(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, _, _ = setup(fixture)
    message = store.post_message(owner["user_id"], owner["organization_id"], topics[2],
                                 f"@[{bot['id']}:test-model] Explain", "model-mention")
    run = runtime.from_message(bot["token"], message["id"])
    assert run["model"] == "test-model"
    with store.transaction(write=True) as db:
        db.execute("UPDATE messages SET content='Edited request' WHERE id=?", (message["id"],))
    assert runtime.from_message(bot["token"], message["id"])["id"] == run["id"]
    bad = store.post_message(owner["user_id"], owner["organization_id"], topics[2],
                            f"@[{bot['id']}:unadvertised] Explain", "bad-model-mention")
    with pytest.raises(StoreError, match="not advertised"):
        runtime.from_message(bot["token"], bad["id"])
    conflicting = store.post_message(owner["user_id"], owner["organization_id"], topics[2],
                                     f"@[{bot['id']}:default] @[{bot['id']}:test-model]", "conflicting-models")
    with pytest.raises(StoreError, match="one model"):
        runtime.from_message(bot["token"], conflicting["id"])


def test_activity_allowlist_bounded_history_and_heartbeat_preserves_phase(fixture, monkeypatch):
    runtime, bot, _, run, _ = setup(fixture)
    claim = runtime.claim(bot["token"])
    now = [1_900_000_000]
    monkeypatch.setattr("reasoning_ledger.server.bot_runtime.time.time", lambda: now[0])
    with runtime.store.transaction(write=True) as db:
        db.execute("UPDATE bot_runs SET updated_at=? WHERE id=?", (now[0], run["id"]))
        db.execute("UPDATE bots SET expires_at=? WHERE id=?", (now[0] + 1000, bot["id"]))
    args = (bot["token"], run["id"], claim["lease"])
    runtime.progress(*args, "writing", "writing_ledger")
    runtime.progress(*args)
    current = runtime.get(bot["created_by"], bot["organization_id"], run["id"])
    assert current["phase"] == "writing" and current["activity"][-1]["label"] == "Writing ledger entry"
    with pytest.raises(StoreError):
        runtime.progress(*args, activity="SECRET arbitrary reasoning")
    for index in range(20):
        now[0] += 2
        runtime.progress(*args, activity="reading_topic" if index % 2 else "writing_ledger")
    assert len(runtime.get(bot["created_by"], bot["organization_id"], run["id"])["activity"]) == 12


def test_edits_before_bot_capture_preserve_original_event_and_request(fixture):
    store, owner, topics, _, _, bots = fixture
    org, user = owner["organization_id"], owner["user_id"]
    bot = bots.create(user, org, "Mention bot", topics[1:], ["messages:read", "messages:write"], personal=True)
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], False, False)
    original = f"@[{bot['id']}] Check the original statement."
    message = store.post_message(user, org, topics[1], original, "captured-edit-prompt")
    store.edit_message(user, org, topics[1], message["id"], "A corrected prompt with no bot mention", 0)
    events = bots.messages(bot["token"])
    assert events[-1]["content"] == original and events[-1]["revision"] == 0
    assert store.messages(user, org, topics[1])[-1]["content"] == "A corrected prompt with no bot mention"
    first = runtime.from_message(bot["token"], message["id"])
    claimed = runtime.claim(bot["token"])
    assert claimed["id"] == first["id"] and claimed["prompt"] == original
    store.edit_message(user, org, topics[1], message["id"], "Another correction", 1)
    assert runtime.from_message(bot["token"], message["id"])["id"] == first["id"]
    never_mentioned = store.post_message(user, org, topics[1], "Ordinary message", "not-originally-mentioned")
    store.edit_message(user, org, topics[1], never_mentioned["id"], original, 0)
    with pytest.raises(StoreError, match="does not mention"):
        runtime.from_message(bot["token"], never_mentioned["id"])
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM bot_runs").fetchone()[0] == 1


def test_owner_only_requests_and_exact_approval_single_consumption(fixture):
    store, owner, topics, _, _, _ = fixture
    other = member(fixture)
    runtime, bot, model, run, _ = setup(fixture, local_tools=True)
    assert runtime.models(other, owner["organization_id"], topics[1]) == []
    with pytest.raises(StoreError, match="Who can use this bot"):
        runtime.enqueue(other, owner["organization_id"], topics[1], model, "outsider", prompt="execute")
    claimed = runtime.claim(bot["token"])
    command = ["python3", "-c", "print(2 + 2)"]
    approval = runtime.request_execution(bot["token"], run["id"], claimed["lease"], command)
    assert runtime.request_execution(bot["token"], run["id"], claimed["lease"], command)["id"] == approval["id"]
    assert runtime.approvals(other, owner["organization_id"]) == []
    with pytest.raises(StoreError):
        runtime.approve(other, owner["organization_id"], approval["id"], True)
    with pytest.raises(StoreError):
        runtime.execution(bot["token"], run["id"], claimed["lease"], approval["id"], consume=True)
    runtime.approve(owner["user_id"], owner["organization_id"], approval["id"], True)
    assert runtime.execution(bot["token"], run["id"], claimed["lease"], approval["id"], consume=True)["status"] == "consumed"
    with pytest.raises(StoreError):
        runtime.execution(bot["token"], run["id"], claimed["lease"], approval["id"], consume=True)


def test_interrupted_invocation_is_never_automatically_reexecuted(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    claimed = runtime.claim(bot["token"])
    with store.transaction(write=True) as db:
        db.execute("UPDATE bot_runs SET updated_at=0 WHERE id=?", (run["id"],))
    with pytest.raises(StoreError, match="heartbeat expired"):
        runtime.progress(bot["token"], run["id"], claimed["lease"], "thinking")
    assert runtime.claim(bot["token"]) is None
    assert runtime.listing(owner["user_id"], owner["organization_id"], topics[1])[0]["status"] == "outcome_unknown"


def test_live_revocation_and_other_requester_permissions(fixture):
    store, owner, topics, _, _, bots = fixture
    other = member(fixture)
    runtime, bot, model, _, _ = setup(fixture, other_users=True)
    run = runtime.enqueue(other, owner["organization_id"], topics[2], model, "other-user", prompt="Research")
    claimed = runtime.claim(bot["token"])
    bots.revoke(owner["user_id"], owner["organization_id"], bot["id"], personal=True)
    with pytest.raises(StoreError):
        runtime.progress(bot["token"], claimed["id"], claimed["lease"], "thinking")
    assert run["requested_by"] == other


def test_mathematical_tools_use_canonical_records_and_live_collection_policy(fixture):
    from reasoning_ledger.server.bot_tools import BotTools

    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    with store.transaction(write=True) as db:
        db.execute("UPDATE topics SET knowledge_policy_json='[\"ledger\",\"bibliography\",\"journal\"]' WHERE id=?", (topics[1],))
    claimed = runtime.claim(bot["token"])
    tools = BotTools(runtime)
    args = (bot["token"], run["id"], claimed["lease"])
    claim = tools.invoke(*args, "ledger_claim", {"statement": "For every integer n, n = n."}, "claim-1")
    assert tools.invoke(*args, "ledger_claim", {"statement": "For every integer n, n = n."}, "claim-1")["claim_id"] == claim["claim_id"]
    detail = tools.invoke(*args, "ledger_read", {"ledger_id": claim["claim_id"]}, "read-1")
    assert detail["claim_id"] == claim["claim_id"]
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM mathematical_records WHERE record_type='claim-head'").fetchone()[0] == 1
        record = db.execute("SELECT body_json FROM mathematical_records WHERE record_type='claim-revision'").fetchone()[0]
        assert "bot/" + bot["id"] in record
    with store.transaction(write=True) as db:
        db.execute("UPDATE topics SET knowledge_policy_json='[]' WHERE id=?", (topics[1],))
    with pytest.raises(StoreError):
        tools.invoke(*args, "ledger_claim", {"statement": "Denied"}, "claim-2")


def test_gateway_confines_paths_and_refuses_unsandboxed_execution(tmp_path, monkeypatch):
    from reasoning_ledger.server.bot_client import BotClientError
    from reasoning_ledger.server.bot_gateway import sandbox_arguments, workspace_file

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "escape").symlink_to(tmp_path)
    for path in ("../secret", "/etc/passwd", "escape/secret"):
        with pytest.raises(BotClientError):
            workspace_file(workspace, path)
    assert workspace_file(workspace, "sub/result.txt") == workspace / "sub/result.txt"
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(BotClientError, match="bubblewrap"):
        sandbox_arguments(workspace, ["python3", "-c", "print(4)"])


def test_public_access_covers_new_topics_but_never_expands_to_private_projects(fixture):
    store, owner, topics, _, _, bots = fixture
    user, org = owner["user_id"], owner["organization_id"]
    bot = bots.create(user, org, "Public bot", [], ["messages:read", "messages:write", "knowledge:read"],
                      public_access=True)
    private = store.create_entity(user, org, "projects", "Private", visibility="private", owner_id=user)
    channel = store.create_entity(user, org, "channels", "Secrets", private["id"])
    secret = store.create_entity(user, org, "topics", "Private topic", channel["id"], "beta")
    with store.transaction() as db:
        public_channel = db.execute("SELECT channel_id FROM topics WHERE id=?", (topics[0],)).fetchone()[0]
    fresh = store.create_entity(user, org, "topics", "New public topic", public_channel, "beta")
    assert fresh["id"] in bots.identity(bot["token"])["topic_ids"]
    assert secret["id"] not in bots.identity(bot["token"])["topic_ids"]
    store.post_message(user, org, fresh["id"], "Public", "fresh")
    store.post_message(user, org, secret["id"], "Secret", "secret")
    assert [message["content"] for message in bots.messages(bot["token"])] == ["Public"]
    with pytest.raises(StoreError):
        bots.post(bot["token"], secret["id"], "Denied", "denied")


def test_execution_disabled_after_approval_cannot_be_consumed(fixture):
    _, owner, _, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture, local_tools=True)
    claim = runtime.claim(bot["token"])
    approval = runtime.request_execution(bot["token"], run["id"], claim["lease"], ["python3", "-c", "print(4)"])
    runtime.approve(owner["user_id"], owner["organization_id"], approval["id"], True)
    runtime.advertise(bot["token"], "codex", ["default", "test-model"], False, False)
    with pytest.raises(StoreError, match="disabled local execution"):
        runtime.execution(bot["token"], run["id"], claim["lease"], approval["id"], consume=True)


def test_approved_execution_output_and_runtime_are_bounded():
    import sys

    from reasoning_ledger.server.bot_gateway import bounded_execute

    result = bounded_execute([sys.executable, "-c", "print('x' * 100000)"], maximum=1024)
    assert result["truncated"] and len(result["output"]) == 1024
    result = bounded_execute([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
    assert result["timed_out"] and result["exit_code"] != 0


def test_authenticated_runtime_api_routes_and_csrf(fixture, monkeypatch):
    import fastapi.dependencies.utils
    import fastapi.routing

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(fastapi.dependencies.utils, "run_in_threadpool", inline)
    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", inline)
    store, owner, topics, _, _, bots = fixture
    bot = bots.create(owner["user_id"], owner["organization_id"], "Local", [topics[1]],
                      ["messages:read", "messages:write"], personal=True)
    message = store.post_message(owner["user_id"], owner["organization_id"], topics[1], "Question", "wire-request")
    app = create_app(Config(store.data_dir, "https://server.example", inference_enabled=False))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://server.example") as client:
            headers = {"authorization": "Bearer " + bot["token"]}
            result = await client.post("/api/bot/runtime", headers=headers,
                                       json={"engine": "codex", "models": ["default"], "local_tools": False})
            assert result.status_code == 200
            root = f"/api/organizations/{owner['organization_id']}/topics/{topics[1]}"
            assert (await client.get(root + "/bot-models")).status_code == 401
            client.cookies.set("__Host-reasoning_session", owner["access_token"])
            models = (await client.get(root + "/bot-models")).json()
            body = {"model": models[0]["id"], "client_id": "wire-runtime", "through_sequence": message["sequence"]}
            assert (await client.post(root + "/bot-runs", json=body)).status_code == 403
            human_headers = {"origin": "https://server.example", "x-csrf-token": owner["csrf_token"]}
            queued = await client.post(root + "/bot-runs", json=body, headers=human_headers)
            assert queued.status_code == 201
            stop_path = f"/api/organizations/{owner['organization_id']}/bot-runs/{queued.json()['id']}/stop"
            assert (await client.post(stop_path, json={})).status_code == 403
            assert (await client.post(stop_path, json={}, headers=human_headers)).json()["status"] == "cancelled"
            queued = await client.post(root + "/bot-runs", json={**body, "client_id": "wire-after-stop"}, headers=human_headers)
            assert queued.status_code == 201
            client.cookies.clear()
            claim_response = await client.post("/api/bot/runs/claim", headers=headers, json={})
            assert claim_response.status_code == 200
            claimed = claim_response.json()
            path = "/api/bot/runs/" + claimed["id"]
            assert (await client.post(path + "/progress", headers=headers,
                                     json={"lease": claimed["lease"], "phase": "thinking"})).status_code == 200
            assert (await client.post(path + "/finish", headers=headers,
                                     json={"lease": claimed["lease"], "status": "succeeded", "output": "Answer"})).status_code == 200
            client.cookies.set("__Host-reasoning_session", owner["access_token"])
            assert "lease" not in (await client.get(root + "/bot-runs")).text

    asyncio.run(check())


def test_on_demand_search_never_delegates_the_bot_owners_private_access(fixture):
    from reasoning_ledger.server.bot_tools import BotTools

    store, owner, topics, _, _, bots = fixture
    user, org = owner["user_id"], owner["organization_id"]
    other = member(fixture)
    private = store.create_entity(user, org, "projects", "Private", visibility="private", owner_id=user)
    channel = store.create_entity(user, org, "channels", "Secrets", private["id"])
    secret = store.create_entity(user, org, "topics", "Private", channel["id"], "alpha")
    bot = bots.create(user, org, "Research", [secret["id"]], ["messages:read", "messages:write"],
                      personal=True, public_access=True)
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], False, False)
    share(fixture, bot["id"])
    store.post_message(user, org, secret["id"], "Needle private", "secret")
    store.post_message(user, org, topics[0], "Needle public", "public")
    run = runtime.enqueue(other, org, topics[1], f"bot/{bot['id']}/default", "search-request", prompt="Find Needle")
    claim = runtime.claim(bot["token"])
    result = BotTools(runtime).invoke(bot["token"], run["id"], claim["lease"], "messages_search", {"query": "Needle"}, "search")
    assert [message["content"] for message in result] == ["Needle public"]


def test_a_bot_reply_lists_the_files_its_run_saved(fixture):
    from reasoning_ledger.server.bot_tools import BotTools

    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    claimed = runtime.claim(bot["token"])
    BotTools(runtime).invoke(bot["token"], run["id"], claimed["lease"], "artifact_write",
                             {"path": "bot-notes.tex", "text": "\\section{Notes}"}, "write-1")
    runtime.finish(bot["token"], run["id"], claimed["lease"], "succeeded", "Saved bot-notes.tex.")
    reply = [message for message in store.messages(owner["user_id"], owner["organization_id"], topics[1])
             if message["author_kind"] == "bot"][-1]
    assert [(file["path"], file["revision"]) for file in reply["files"]] == [("bot-notes.tex", 1)]
