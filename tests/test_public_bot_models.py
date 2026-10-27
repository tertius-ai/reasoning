"""Live catalog discovery and exact bot model routing without paid inference.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import os
import sys
import threading

import pytest
from test_public_bots import fixture as bot_fixture

from reasoning_ledger.server import bot_models
from reasoning_ledger.server.bot_agent import agent_arguments
from reasoning_ledger.server.bot_runner import runtime_step
from reasoning_ledger.server.bot_runtime import BotRuntime, mentioned_models
from reasoning_ledger.server.store import StoreError


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


def cli(tmp_path, monkeypatch, engine, source):
    binary = tmp_path / engine
    binary.write_text(f"#!{sys.executable}\n" + source)
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_metadata_only_subprocess_protocol(tmp_path, monkeypatch, engine):
    if engine == "codex":
        source = '''
import json, sys
assert sys.argv[1:] == ['debug', 'models']
print(json.dumps({'models': [{'slug':'discovered','display_name':'Discovered',
 'visibility':'list','supported_reasoning_levels':[{'effort':'ultra'}]},
 {'slug':'hidden','visibility':'hide'}, {'slug':'bad id','visibility':'list'}]}))
'''
    else:
        source = '''
import json, sys, time
assert '--no-session-persistence' in sys.argv and '--strict-mcp-config' in sys.argv
assert json.loads(sys.argv[sys.argv.index('--settings')+1]) == {'disableAllHooks':True}
assert sys.argv[sys.argv.index('--setting-sources')+1] == ''
assert sys.argv[sys.argv.index('--tools')+1] == ''
assert json.loads(sys.argv[sys.argv.index('--mcp-config')+1]) == {'mcpServers':{}}
request = json.loads(sys.stdin.readline())
assert request == {'type':'control_request','request_id':'model-discovery',
 'request':{'subtype':'initialize','hooks':None}}
print(json.dumps({'type':'control_response','response':{'subtype':'success',
 'request_id':request['request_id'],'response':{'models':[{'value':'opus[1m]',
 'displayName':'Opus (1M context)','supportedEffortLevels':['high','max','invented']}]}}}),flush=True)
time.sleep(60)  # Discovery must stop after metadata; stdin is never a user prompt.
'''
    cli(tmp_path, monkeypatch, engine, source)
    assert bot_models.discover(engine) == (
        {"discovered": {"name": "Discovered", "efforts": ["ultra"]}} if engine == "codex" else
        {"opus[1m]": {"name": "Opus (1M context)", "efforts": ["high", "max"]}})


@pytest.mark.parametrize("source", ["print('invalid JSON')", "import sys; sys.exit(1)",
                                    "print('x'*10000)", "import time; time.sleep(60)"])
def test_failed_oversized_or_stalled_discovery_is_bounded(tmp_path, monkeypatch, source):
    cli(tmp_path, monkeypatch, "codex", source)
    monkeypatch.setattr(bot_models, "MAX_OUTPUT", 1000)
    monkeypatch.setattr(bot_models, "PROBE_SECONDS", 0.1)
    with pytest.raises(ValueError):
        bot_models.discover("codex")


def test_refresh_never_blocks_heartbeats_and_retains_last_success(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    catalog = bot_models.ModelDiscovery("codex")
    calls = []

    def discover(engine):
        calls.append(engine)
        entered.set()
        assert release.wait(2)
        return {"new": {"name": "New model", "efforts": ["high"]}}

    monkeypatch.setattr(bot_models, "discover", discover)
    original = catalog._refresh

    def refresh():
        try:
            original()
        finally:
            finished.set()

    monkeypatch.setattr(catalog, "_refresh", refresh)
    models, capabilities = catalog.advertisement(["default"])
    assert entered.wait(2)
    assert models == ["default"] and capabilities["efforts"] == {"default": []}
    assert catalog.advertisement()[0] == ["default"] and calls == ["codex"]
    release.set()
    assert finished.wait(2)
    assert catalog.advertisement()[0] == ["default", "new"]
    assert catalog.advertisement(["new"])[0] == ["new"], "explicit allowlists remain restricted"
    assert catalog.advertisement()[1]["efforts"]["default"] == [], "do not guess Codex's configured default effort"
    next_refresh = catalog._next_refresh
    monkeypatch.setattr(bot_models.time, "monotonic", lambda: next_refresh - 1)
    assert catalog.advertisement()[0] == ["default", "new"] and calls == ["codex"]
    monkeypatch.setattr(bot_models, "discover", lambda _: (_ for _ in ()).throw(ValueError("PRIVATE STDERR")))
    catalog._refresh()
    assert catalog.advertisement()[0] == ["default", "new"]
    assert catalog._next_refresh == next_refresh - 1 + bot_models.RETRY_SECONDS


def test_catalog_size_and_empty_malformed_metadata(monkeypatch):
    catalog = bot_models.ModelDiscovery("claude")
    catalog._catalog = {str(i): {"name": str(i), "efforts": []} for i in range(30)}
    catalog._next_refresh = float("inf")
    assert len(catalog.advertisement()[0]) == 20
    assert catalog.advertisement()[0][0] == "default"
    for document in ({}, {"models": []}, {"models": [None]}, {"models": "bad"}):
        monkeypatch.setattr(bot_models, "_probe", lambda _, document=document: document)
        with pytest.raises(ValueError):
            bot_models.discover("claude")


@pytest.mark.parametrize(("engine", "model", "effort"), [("codex", "gpt-discovered", "ultra"), ("claude", "opus[1m]", "max")])
def test_discovered_beta_selection_reaches_cli_and_survives_catalog_change(fixture, tmp_path, monkeypatch, engine, model, effort):
    store, owner, topics, _, _, bots = fixture
    org, user = owner["organization_id"], owner["user_id"]
    bot = bots.create(user, org, "My CLI", topics[1:], ["messages:read", "messages:write"], personal=True, public_access=True)
    runtime = BotRuntime(bots)
    root, workspace = tmp_path / "private", tmp_path / "workspace"
    root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    discovery = bot_models.ModelDiscovery(engine)
    monkeypatch.setattr(bot_models, "discover", lambda _: {model: {"name": "Discovered model", "efforts": [effort]}})
    discovery._refresh()

    class Client:
        def messages(self, after, limit):
            return bots.messages(bot["token"], after, limit)

        def _request(self, method, path, body=None):
            if path == "/api/bot/runtime":
                return runtime.advertise(bot["token"], **body)
            if path == "/api/bot/runs/claim":
                return runtime.claim(bot["token"])
            run_id = path.split("/")[4]
            if path.endswith("/progress"):
                return runtime.progress(bot["token"], run_id, **body)
            return runtime.finish(bot["token"], run_id, **body)

    calls = []

    def runner(engine, cwd, prompt, local_tools, session, **options):
        calls.append(options)
        args = agent_arguments(engine, cwd, model=options["model"], effort=options["effort"])
        assert args[args.index("--model") + 1] == model
        assert ('model_reasoning_effort="ultra"' in args if engine == "codex" else args[args.index("--effort") + 1] == effort)
        # A refresh can remove this model from new choices without rewriting the admitted invocation.
        discovery._catalog = {"replacement": {"name": "Replacement", "efforts": []}}
        options["heartbeat"]()
        assert options["model"] == model
        return {"text": "Selected model answered", "session": None}

    agent = {"engine": engine, "workspace": str(workspace), "local_tools": False}
    binding = {"server": "https://example.org", "bot_id": bot["id"], "owner_id": user, "allow_other_users": False}
    config = {"server": binding["server"], "token": bot["token"]}
    assert not runtime_step(Client(), root, config, agent, binding, discovery=discovery)["replied"]
    choices = runtime.models(user, org, topics[1])
    [choice] = [item for item in choices if item["id"].endswith("/" + model)]
    assert choice["name"] == "My CLI · Discovered model" and choice["efforts"] == [effort]
    with store.transaction() as db:
        channel = db.execute("SELECT channel_id FROM topics WHERE id=?", (topics[1],)).fetchone()[0]
    assert any(item["id"] == choice["id"] for item in runtime.channel_models(user, org, channel))
    message = store.post_message(user, org, topics[1], "Answer this", "selected-beta-model")
    runtime.enqueue(user, org, topics[1], choice["id"], "selected-request", through=message["sequence"], effort=effort)
    assert runtime_step(Client(), root, config, agent, binding, runner=runner, discovery=discovery)["replied"]
    assert len(calls) == 1
    assert runtime.listing(user, org, topics[1])[0]["model"] == model
    assert runtime.models(user, org, topics[1])[1]["id"].endswith("/replacement")
    mention = f"@[{bot['id']}:{model}!{effort}]"
    assert mentioned_models(mention, bot["id"]) == [model]
    with pytest.raises(StoreError, match="not advertised"):
        runtime.enqueue(user, org, topics[1], choice["id"], "removed-model", through=message["sequence"], effort=effort)


def test_model_label_validation(fixture):
    _, owner, topics, _, _, bots = fixture
    bot = bots.create(owner["user_id"], owner["organization_id"], "Bot", topics[1:], ["messages:read", "messages:write"])
    runtime = BotRuntime(bots)
    for names in ({"unknown": "Name"}, {"default": "x" * 101}, {"default": "bad\nlabel"}, {"default": {}}, []):
        with pytest.raises(StoreError, match="model names"):
            runtime.advertise(bot["token"], "codex", ["default"], False, False, {"model_names": names})
