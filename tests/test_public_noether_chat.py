"""Noether discovery, approvals and returned evidence without paid execution or rebuilt images."""

import base64
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from test_public_compute import IMAGE
from test_public_server import installation as installation  # noqa: F401
from test_public_topic_agents import finish, root

from reasoning_ledger.server.bot_compute import BotCompute
from reasoning_ledger.server.bot_runtime import BotRuntime
from reasoning_ledger.server.bot_store import BotStore
from reasoning_ledger.server.bot_tools import BotTools
from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES, ComputeImage
from reasoning_ledger.server.compute_store import ComputeStore
from reasoning_ledger.server.noether_compute import operations, protocol, submission
from reasoning_ledger.server.research_agent import ResearchAgent, TurnSuspended
from reasoning_ledger.server.research_tools import DEFAULT_TOOLS, ResearchProposals, ResearchTools
from reasoning_ledger.server.store import StoreError
from reasoning_ledger.server.topic_agents import TopicAgents


@pytest.fixture
def environment(installation):
    state, client, owner, topics, parent, quote = root(installation)
    image = ComputeImage.parse({**IMAGE, "languages": ["python", "sage"]})
    compute = ComputeStore(state.store, {image.id: image})
    compute.set_policy(owner["user_id"], owner["organization_id"], [image.id], 10000, 2)
    return state, owner, topics, parent, quote, compute, image


def evidence(compute, owner, image, result):
    worker = compute.register_worker(owner["user_id"], owner["organization_id"], "Fixture delivery", [image.id], image.maximum_resources)
    claimed = compute.claim(worker["token"])
    args = (worker["token"], claimed["id"], claimed["attempt_id"], claimed["lease"])
    text = json.dumps(result)
    compute.append_log(*args, 0, text)
    body = text.encode()
    artifact = compute.begin_artifact(*args, "noether-result.json", len(body), "sha256:" + hashlib.sha256(body).hexdigest())
    compute.artifact_chunk(*args, artifact["id"], 0, base64.b64encode(body).decode())
    compute.commit_artifact(*args, artifact["id"])
    compute.finish(*args, "succeeded", 1, 0, None)
    return claimed, artifact


def test_discovery_uses_worker_literal_schemas_without_importing_or_running_noether(environment):
    state, owner, topics, parent, _, compute, _ = environment
    tools = ResearchTools(state, parent, compute=compute)
    found = tools.invoke("tool_search", {"query": "noether"})["result"]
    assert {item["name"] for item in found} >= {"noether_operations", "noether_compute", "noether_status", "noether_describe"}
    catalog = tools.invoke("noether_operations", {})["result"]
    assert catalog["runtime_verified"] is False
    assert {item["name"] for item in catalog["operations"]} == set(protocol()[1])
    for operation in catalog["operations"]:
        detail = tools.invoke("noether_operations", {"operation": operation["name"]})["result"]["operation"]
        assert detail["example"]["operation"] == detail["name"]
        assert any(Draft202012Validator(schema).is_valid(detail["example"]["payload"]) for schema in detail["input_schemas"])
    assert compute.jobs(owner["user_id"], owner["organization_id"], topics[1]["id"]) == []
    with pytest.raises(StoreError, match="executable operation"):
        operations("numthy.factor")
    with pytest.raises(StoreError, match="unsupported Noether wrapper"):
        submission("noether_compute", {"operation": "numthy.factor", "payload": {}})


@pytest.mark.parametrize("tool", ["noether_status", "noether_compute", "noether_describe"])
@pytest.mark.parametrize("allowance", [0, 1000])
def test_native_noether_waits_then_continues_with_readable_logs_and_json(environment, tool, allowance):
    state, owner, topics, parent, quote, compute, image = environment
    with state.store.transaction(write=True) as db:
        db.execute("INSERT INTO research_topic_settings(organization_id,topic_id,tools_json,compute_allowance_cpu_seconds) "
                   "VALUES(?,?,?,?)", (owner["organization_id"], topics[1]["id"], json.dumps(DEFAULT_TOOLS), allowance))
    tools = ResearchTools(state, parent, compute=compute)
    arguments = {"image_id": image.id, "resources": DEFAULT_RESOURCES}
    if tool != "noether_status":
        arguments["operation"] = "factor_integer"
    if tool == "noether_compute":
        arguments["payload"] = {"value": -360}
    returned = tools.invoke(tool, arguments)["result"]
    agent = ResearchAgent(state, None, parent, quote, compute=compute)
    with pytest.raises(TurnSuspended, match="delegated work"):
        agent._transform({"messages": []})
    finish(state, parent, "Awaiting the exact Noether result")
    agents = TopicAgents(state, compute)
    agents.dispatch()
    assert state.claim() is None
    if not allowance:
        assert compute.jobs(owner["user_id"], owner["organization_id"], topics[1]["id"]) == []
        job = ResearchProposals(state, compute, None).review(owner["user_id"], owner["organization_id"],
            topics[1]["id"], returned["proposal_id"], True)["result"]
    else:
        job = returned
    packet = {"ok": True, "result": {"fixture_evidence": "exact retained Noether response"}}
    claimed, artifact = evidence(compute, owner, image, packet)
    assert claimed["id"] == job["id"] and "noether-result.json" in claimed["parameters"]["program"]
    agents.dispatch()
    resumed = state.claim()
    assert resumed is not None
    tools = ResearchTools(state, resumed, compute=compute)
    result = tools.invoke("compute_result", {"job_id": job["id"]})["result"]
    assert result["job"]["status"] == "succeeded" and result["logs_tool"] == "compute_log_read"
    assert result["artifacts"][0]["name"] == "noether-result.json"
    logs = tools.invoke("compute_log_read", {"job_id": job["id"]})["result"]
    assert json.loads(logs["text"]) == packet and logs["truncated"] is False
    saved = tools.invoke("artifact_read", {"job_id": job["id"], "artifact_id": artifact["id"]})["result"]
    assert json.loads(saved["text"]) == packet
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM inference_calls").fetchone()[0] == 0


@pytest.mark.parametrize("failure", [None, "worker_error", "invalid_json", "changed_worker"])
def test_submitted_program_preserves_protocol_and_json_without_changing_worker(tmp_path, monkeypatch, failure):
    worker = Path(__file__).parents[1] / "src/reasoning_ledger/agents/noether_worker.py"
    before = worker.read_bytes()
    installed = tmp_path / "worker.py"
    installed.write_bytes(before + b"\n# different installed contract\n" if failure == "changed_worker" else before)
    output = tmp_path / "noether-result.json"
    expected = {"ok": True, "result": {"value": -360, "unit": -1, "factors": [[2, 3], [3, 2], [5, 1]], "certificate_verified": True}}
    calls = []

    def run(args, **options):
        calls.append((args, options))
        assert json.loads(options["input"]) == {"operation": "factor_integer", "payload": {"value": -360}}
        response = {"ok": False, "error": "fixture worker failure"} if failure == "worker_error" else expected
        return SimpleNamespace(returncode=0, stdout="invalid fixture response" if failure == "invalid_json" else json.dumps(response),
                               stderr="")

    def paths(path):
        return installed if path == "/opt/reasoning/noether_worker.py" else output

    request = submission("noether_compute", {"operation": "factor_integer", "payload": {"value": -360},
                         "resources": DEFAULT_RESOURCES, "image_id": "research-cpu"})
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", run)
        patch.setattr("pathlib.Path", paths)
        with pytest.raises(SystemExit) as finished:
            exec(compile(request["program"], "<submitted Noether program>", "exec"), {})
    if failure == "changed_worker":
        assert "protocol differs" in finished.value.code and not calls and not output.exists()
    else:
        assert finished.value.code == (1 if failure else 0) and len(calls) == 1
        result = json.loads(output.read_text())
        assert result == expected if failure is None else result["ok"] is False
    assert worker.read_bytes() == before and protocol()[0] == hashlib.sha256(before).hexdigest()


def test_bot_noether_is_discoverable_but_uses_exact_owner_approval_and_result_access(environment):
    state, owner, topics, _, _, compute, image = environment
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    bots = BotStore(state.store)
    bot = bots.create(user, org, "Noether bot", [topic], ["messages:read", "messages:write", "knowledge:read", "knowledge:write"],
                      personal=True)
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], False, False)
    message = state.store.post_message(user, org, topic, "Use Noether", "bot-noether-message")
    runtime.enqueue(user, org, topic, f"bot/{bot['id']}/default", "bot-noether-request", through=message["sequence"])
    run = runtime.claim(bot["token"])
    tools = BotTools(runtime, compute=compute)
    args = (bot["token"], run["id"], run["lease"])
    names = {item["name"] for item in tools.context(*args)["tools"]}
    assert {"noether_operations", "noether_compute", "noether_status", "noether_describe", "compute_log_read"} <= names
    catalog = tools.invoke(*args, "noether_operations", {}, "catalog")
    assert any(item["name"] == "factor_integer" for item in catalog["operations"])
    approval = tools.invoke(*args, "noether_compute", {"operation": "factor_integer", "payload": {"value": -360},
                            "image_id": image.id, "resources": DEFAULT_RESOURCES}, "compute")
    assert approval["status"] == "pending" and compute.jobs(user, org, topic) == []
    job = BotCompute(runtime, compute).decide(user, org, approval["id"], True)
    runtime.finish(bot["token"], run["id"], run["lease"], "succeeded", output="Awaiting the approved Noether result")
    assert runtime.claim(bot["token"]) is None
    claimed, _ = evidence(compute, owner, image, {"ok": True, "result": {"fixture": "worker response"}})
    assert claimed["id"] == job["job_id"]
    integration = runtime.claim(bot["token"])
    assert integration and job["job_id"] in integration["prompt"]
    args = (bot["token"], integration["id"], integration["lease"])
    logs = tools.invoke(*args, "compute_log_read", {"job_id": job["job_id"]}, "logs")
    assert json.loads(logs["text"])["ok"] is True
    bots.update(user, org, bot["id"], "Noether bot", [topic], ["messages:read", "messages:write", "knowledge:read"], personal=True)
    names = {item["name"] for item in tools.context(*args)["tools"]}
    assert "noether_operations" in names and not names & {"noether_compute", "noether_status", "noether_describe"}
    with pytest.raises(StoreError):
        tools.invoke(*args, "noether_status", {"image_id": image.id, "resources": DEFAULT_RESOURCES}, "revoked-write")


def test_log_pages_are_lossless_under_json_escaping_and_recheck_project_and_membership(environment):
    state, owner, topics, parent, _, compute, image = environment
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    parameters = compute.parameters(image.id, "python", "print('fixture')", DEFAULT_RESOURCES)
    job = compute.enqueue(user, org, topic, "large-logs", parameters)
    worker = compute.register_worker(user, org, "Log fixture", [image.id], image.maximum_resources)
    claimed = compute.claim(worker["token"])
    text = 'α"\\\n\x00' * 6000
    for index, start in enumerate(range(0, len(text), 2000)):
        compute.append_log(worker["token"], claimed["id"], claimed["attempt_id"], claimed["lease"], index, text[start:start + 2000])
    tools, offset, observed = ResearchTools(state, parent, compute=compute), 0, ""
    while True:
        page = tools.invoke("compute_log_read", {"job_id": job["id"], "offset": offset})["result"]
        assert len(json.dumps(page, ensure_ascii=False)) < 16000
        observed += page["text"]
        if not page["truncated"]:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert observed == text and page["total_characters"] == len(text)
    other_project = state.store.create_entity(user, org, "projects", "Other project")
    other_channel = state.store.create_entity(user, org, "channels", "other", parent_id=other_project["id"])
    other_topic = state.store.create_entity(user, org, "topics", "Other beta", parent_id=other_channel["id"], topic_kind="beta")
    other = compute.enqueue(user, org, other_topic["id"], "other-job", parameters)
    with pytest.raises(ValueError, match="outside this topic"):
        tools.invoke("compute_log_read", {"job_id": other["id"]})
    with state.store.transaction(write=True) as db:
        db.execute("DELETE FROM memberships WHERE user_id=? AND organization_id=?", (user, org))
    with pytest.raises(StoreError):
        compute.log_text(user, org, job["id"])
