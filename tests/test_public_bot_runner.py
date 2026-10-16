"""Local CLI runner wire tests, with a free completed-answer fixture.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import json

import pytest
from test_public_bots import fixture as bot_fixture

from reasoning_ledger.deployment.private import read_document
from reasoning_ledger.server.bot_client import BotClientError, BotUnavailable
from reasoning_ledger.server.bot_runner import runtime_step
from reasoning_ledger.server.bot_runtime import BotRuntime
from reasoning_ledger.server.bot_tools import BotTools


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


@pytest.mark.parametrize("lost_receipt", [False, True])
@pytest.mark.parametrize("heartbeat_failures", [0, 1, 3])
def test_mention_runner_model_routing_canonical_tools_and_post_receipt(fixture, tmp_path, monkeypatch,
                                                                      lost_receipt, heartbeat_failures):
    monkeypatch.setattr("reasoning_ledger.server.bot_client.time.sleep", lambda _: None)
    store, owner, topics, _, _, bots = fixture
    bot = bots.create(owner["user_id"], owner["organization_id"], "Math bot", topics[1:],
                      ["messages:read", "messages:write", "knowledge:read", "knowledge:write"], personal=True)
    runtime, calls = BotRuntime(bots), []
    root, workspace = tmp_path / "private", tmp_path / "workspace"
    root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    store.post_message(owner["user_id"], owner["organization_id"], topics[1], f"@[{bot['id']}] Prove n=n", "mention")

    class Client:
        fail_finish = lost_receipt
        failed_heartbeats = heartbeat_failures
        def messages(self, after, limit):
            return bots.messages(bot["token"], after, limit)

        def _request(self, method, path, body=None):
            if path == "/api/bot/runtime":
                return runtime.advertise(bot["token"], **body)
            if path == "/api/bot/runs/from-message":
                return runtime.from_message(bot["token"], body["message_id"])
            if path == "/api/bot/runs/claim":
                return runtime.claim(bot["token"])
            if path.endswith("/progress"):
                if self.failed_heartbeats:
                    self.failed_heartbeats -= 1
                    raise BotUnavailable("heartbeat connection lost")
                return runtime.progress(bot["token"], path.split("/")[4], **body)
            result = runtime.finish(bot["token"], path.split("/")[4], **body)
            if self.fail_finish:
                self.fail_finish = False
                raise BotUnavailable("completion receipt lost")
            return result

    def runner(engine, cwd, prompt, local_tools, session, **options):
        calls.append(options)
        assert engine == "codex" and not local_tools and session is None
        assert cwd.is_relative_to(workspace) and "Prove n=n" in prompt
        gateway = read_document(options["gateway"])
        assert not options["gateway"].is_relative_to(cwd)
        assert bot["token"] not in prompt
        assert read_document(root / "runtime-runner.json")["pending"]["phase"] == "executing"
        options["heartbeat"]()
        with store.transaction(write=True) as db:
            db.execute("UPDATE topics SET knowledge_policy_json='[\"ledger\"]' WHERE id=?", (topics[1],))
        tools = BotTools(runtime)
        claim = tools.invoke(bot["token"], gateway["run_id"], gateway["lease"], "ledger_claim", {"statement": "n=n"}, "c1")
        return {"text": "Recorded " + claim["claim_id"], "session": "a" * 32}

    agent = {"engine": "codex", "workspace": str(workspace), "local_tools": False, "models": ["test-model"]}
    binding = {"server": "https://example.org", "bot_id": bot["id"], "owner_id": owner["user_id"], "allow_other_users": False}
    configuration = {"server": binding["server"], "token": bot["token"]}
    client = Client()
    if heartbeat_failures == 3:
        with pytest.raises(BotUnavailable):
            runtime_step(client, root, configuration, agent, binding, runner=runner)
        assert read_document(root / "runtime-runner.json")["pending"]["phase"] == "executing"
        with pytest.raises(BotClientError, match="unknown outcome"):
            runtime_step(client, root, configuration, agent, binding, runner=runner)
        assert len(calls) == 1
        assert runtime.listing(owner["user_id"], owner["organization_id"], topics[1])[0]["status"] == "outcome_unknown"
        return
    if lost_receipt:
        with pytest.raises(BotUnavailable):
            runtime_step(client, root, configuration, agent, binding, runner=runner)
        assert read_document(root / "runtime-runner.json")["pending"]["phase"] == "reply_ready"
    assert runtime_step(client, root, configuration, agent, binding, runner=runner)["replied"]
    assert not runtime_step(client, root, configuration, agent, binding, runner=runner)["replied"]
    assert len(calls) == 1 and calls[0]["model"] == "test-model"
    assert read_document(root / "runtime-runner.json")["pending"] is None
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM bot_runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0] == 0
        assert "bot/" + bot["id"] in json.dumps([dict(row) for row in db.execute("SELECT * FROM mathematical_records")])


@pytest.mark.parametrize("server_feedback", [True, False])
def test_owner_only_policy_records_a_visible_refusal_without_invoking_the_cli(fixture, tmp_path, server_feedback):
    from test_public_bots import member

    store, owner, topics, _, _, bots = fixture
    org = owner["organization_id"]
    requester = member(fixture)
    bot = bots.create(owner["user_id"], org, "Owner bot", topics[1:], ["messages:read", "messages:write"], personal=True)
    runtime, forwarded = BotRuntime(bots), []
    root, workspace = tmp_path / "private", tmp_path / "workspace"
    root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    message = store.post_message(requester, org, topics[1], f"@[{bot['id']}] Please open an issue", "other-mention")

    class Client:
        def messages(self, after, limit):
            return bots.messages(bot["token"], after, limit)

        def _request(self, method, path, body=None):
            if path == "/api/bot/runtime":
                advertised = runtime.advertise(bot["token"], **body)
                return advertised if server_feedback else {key: advertised[key] for key in ("models", "policy")}
            if path == "/api/bot/runs/from-message":
                forwarded.append(body["message_id"])
                return runtime.from_message(bot["token"], body["message_id"])
            if path == "/api/bot/runs/claim":
                return runtime.claim(bot["token"])
            raise AssertionError(path)

    def runner(*arguments, **options):
        raise AssertionError("a refused mention never starts the CLI")

    agent = {"engine": "codex", "workspace": str(workspace), "local_tools": False, "models": ["default"]}
    binding = {"server": "https://example.org", "bot_id": bot["id"], "owner_id": owner["user_id"], "allow_other_users": False}
    result = runtime_step(Client(), root, {"server": binding["server"], "token": bot["token"]}, agent, binding, runner=runner)
    assert result == {"replied": False, "cursor": message["sequence"]}
    runs = runtime.listing(requester, org, topics[1])
    if not server_feedback:
        assert forwarded == [] and runs == []
        return
    assert forwarded == [message["id"]]
    [declined] = runs
    assert declined["status"] == "cancelled" and declined["error_code"] == "requester_not_permitted"
    assert declined["requested_by"] == requester and declined["context_through_sequence"] == message["sequence"]
    assert declined["output_text"] == ""
    with store.transaction() as db:
        assert db.execute("SELECT prompt FROM bot_runs WHERE id=?", (declined["id"],)).fetchone()[0] == ""
    # A lost response is retried idempotently and never queues work.
    assert runtime.from_message(bot["token"], message["id"])["id"] == declined["id"]
    assert runtime.claim(bot["token"]) is None
    # When the owner trusts other members, a new mention is admitted normally.
    runtime.advertise(bot["token"], "codex", ["default"], True, False)
    admitted = store.post_message(requester, org, topics[1], f"@[{bot['id']}] Now please", "other-mention-2")
    queued = runtime.from_message(bot["token"], admitted["id"])
    assert queued["status"] == "queued" and queued["requested_by"] == requester
