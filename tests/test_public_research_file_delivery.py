"""Large source delivery and explicit diagnostic recovery through real HTTP routes.

Only the remote provider transport is synthetic; no paid generation is used.
"""

import hashlib
import json

import httpx
import pytest
from test_organization_research_workbench import source_run
from test_public_inference import prepare
from test_public_provider_stream import WireBody, artifact_stream, content, event, synthetic_latex, usage
from test_public_server import installation as installation
from test_public_server import invited

from reasoning_ledger.server.openrouter import OpenRouter, Quote
from reasoning_ledger.server.research_agent import ResearchAgent
from reasoning_ledger.server.research_artifact_recovery import _copy_path, _file_arguments
from reasoning_ledger.server.research_artifacts import ResearchArtifacts, safe_path
from reasoning_ledger.server.research_file_limits import MAX_ARTIFACT_TEXT_BYTES


def generate_fixture_file(installation, *, interrupted=False, incomplete_json=False, failed_final=False):
    state, owner, topics, row, _ = source_run(installation)
    source = synthetic_latex()
    assert len(source) > 90609
    arguments = json.dumps({"name": "artifact_write", "arguments": {"path": "synthetic-survey.tex", "text": source}},
                           ensure_ascii=False)
    if incomplete_json:
        arguments = arguments[:-20]
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "POST"
        if len(requests) == 1:
            wire = artifact_stream(arguments, done=not interrupted)
            if interrupted:
                wire = event(content("Drafting the requested source file.")) + wire
            if not interrupted:
                receipt = usage()
                receipt["choices"] = []
                wire = wire.replace(b"data: [DONE]", event(receipt) + b"data: [DONE]")
        else:
            assert not interrupted, "recovery must not make another provider call"
            final = usage()
            if failed_final:
                final["choices"][0]["finish_reason"] = "content_filter"
                wire = event(final) + b"data: [DONE]\n\n"
            else:
                wire = event(content("The requested source file is saved.")) + event(final) + b"data: [DONE]\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=WireBody(wire, 8192))

    provider = OpenRouter(transport=httpx.MockTransport(respond))
    # This test isolates delivery from the separate context-management work.
    quote = Quote(**{**json.loads(row["quote_json"]), "context_length": 1_000_000})
    row["quote_json"] = json.dumps(quote.record())
    with state.store.transaction(write=True) as db:
        db.execute("UPDATE llm_runs SET quote_json=? WHERE id=?", (row["quote_json"], row["id"]))
    ResearchAgent(state, provider, row, quote).execute()
    root = f"/api/organizations/{owner['organization_id']}/topics/{topics[1]['id']}/research-files"
    return state, installation[2], owner, topics, row, root, source, requests


def test_large_latex_survives_native_stream_tool_receipt_reply_and_pinned_download(installation):
    state, client, owner, topics, row, root, source, requests = generate_fixture_file(installation)
    run = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert run["status"] == "succeeded" and len(requests) == 2
    files = client.get(root).json()
    assert len(files) == 1 and files[0]["path"] == "synthetic-survey.tex"
    saved = files[0]
    download = client.get(saved["download_url"])
    assert download.content == source.encode("utf8")
    assert download.headers["ETag"] == '"' + hashlib.sha256(source.encode()).hexdigest() + '"'
    assert "attachment" in download.headers["Content-Disposition"]
    replies = state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"])
    reply = next(message for message in replies if message.get("inference_run_id") == row["id"])
    assert reply["files"][0]["sha256"] == saved["sha256"]
    assert reply["files"][0]["revision"] == 1
    preview = client.get(root + "/read", params={"path": saved["path"]}).json()
    assert preview["truncated"] and preview["text"] == source[:16000]


def test_saved_file_is_delivered_even_when_the_final_model_answer_fails(installation):
    state, client, owner, topics, row, root, source, requests = generate_fixture_file(installation, failed_final=True)
    run = state.run(owner["user_id"], owner["organization_id"], row["id"])
    assert run["status"] == "failed" and run["error_code"] == "agent_content_filtered"
    replies = state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"])
    reply = next(message for message in replies if message.get("inference_run_id") == row["id"])
    assert reply["content"].startswith("Saved project files:")
    assert "Partial response: failed" in reply["content"]
    assert client.get(reply["files"][0]["download_url"]).content == source.encode()
    assert client.get(root + "/recovery").json() == [] and len(requests) == 2


@pytest.mark.parametrize("wrapped", [False, True])
def test_finishing_sections_attaches_the_saved_revision_without_an_extra_model_answer(installation, wrapped):
    state, owner, topics, row, tools = source_run(installation)
    draft = tools.invoke("artifact_begin", {"path": "sections.tex", "client_id": "sections-reply"})["result"]
    source = "\\documentclass{article}\nA saved section with α and 研究.\n"
    progress = tools.invoke("artifact_append", {"draft_id": draft["draft_id"], "offset": 0, "text": source})["result"]
    arguments = {"draft_id": draft["draft_id"], "sha256": progress["sha256"]}
    if wrapped:
        saved = tools.invoke("tool_call", {"name": "artifact_finish", "arguments": arguments})["result"]["result"]
    else:
        saved = tools.invoke("artifact_finish", arguments)["result"]
    state.finish(row, "succeeded", no_charge=True)
    messages = state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"])
    reply = next(message for message in messages if message.get("inference_run_id") == row["id"])
    assert reply["content"].startswith("Saved project files:")
    assert [(file["path"], file["revision"]) for file in reply["files"]] == [("sections.tex", saved["revision"])]
    assert installation[2].get(reply["files"][0]["download_url"]).content == source.encode()


@pytest.mark.parametrize("incomplete_json", [False, True])
def test_interrupted_file_is_retained_and_only_complete_source_can_be_recovered(installation, incomplete_json):
    state, client, owner, topics, row, root, source, requests = generate_fixture_file(
        installation, interrupted=True, incomplete_json=incomplete_json)
    assert state.run(owner["user_id"], owner["organization_id"], row["id"])["status"] == "failed"
    assert client.get(root).json() == [] and len(requests) == 1
    with state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM research_tool_receipts").fetchone()[0] == 0
    candidates = client.get(root + "/recovery").json()
    if incomplete_json:
        assert candidates == []
        return
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert "text" not in candidate and candidate["recovery_path"] != candidate["path"]
    # A repaired original is independent of explicit recovery from diagnostic evidence.
    repaired = client.post(root, json={"path": candidate["path"], "text": "User's corrected source."}).json()
    body = {key: candidate[key] for key in ("event_sequence", "tool_index", "sha256")}
    assert client.post(root + "/recovery", json={**body, "sha256": "0" * 64}).status_code == 409
    assert client.post(root + "/recovery", json={**body, "path": candidate["path"]}).status_code == 409
    recovered = client.post(root + "/recovery", json=body)
    assert recovered.status_code == 200
    saved = recovered.json()
    assert client.get(saved["download_url"]).content == source.encode()
    assert client.get(repaired["download_url"]).content == b"User's corrected source."
    assert client.post(root + "/recovery", json=body).json() == saved
    # Retrying a recovery returns its original pinned revision after later edits too.
    client.post(root, json={"path": saved["path"], "text": "A later edit.", "expected_sha256": saved["sha256"]}).raise_for_status()
    assert client.post(root + "/recovery", json=body).json() == saved
    assert client.get(saved["download_url"]).content == source.encode()
    assert client.get(root + "/recovery").json() == [] and len(requests) == 1
    replies = state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"])
    # The failed reply also exposes any source explicitly recovered from its run.
    reply = next(message for message in replies if message.get("inference_run_id") == row["id"])
    assert reply["files"][0]["sha256"] == saved["sha256"]


def test_file_routes_enforce_utf8_byte_budget_and_resumable_offsets(installation):
    _, _, client, owner, topics = prepare(installation)
    root = f"/api/organizations/{owner['organization_id']}/topics/{topics[1]['id']}/research-files"
    source = "α" * (MAX_ARTIFACT_TEXT_BYTES // 2)
    saved = client.post(root, json={"path": "boundary.tex", "text": source})
    assert saved.status_code == 200 and client.get(saved.json()["download_url"]).content == source.encode()
    # A valid source can expand sixfold on the wire; middleware must budget JSON too.
    escaped = "\x01" * MAX_ARTIFACT_TEXT_BYTES
    wire = json.dumps({"path": "escaped.txt", "text": escaped}).encode()
    response = client.post(root, content=wire, headers={"Content-Type": "application/json"})
    assert response.status_code == 200 and client.get(response.json()["download_url"]).content == escaped.encode()
    invalid = client.post(root, content=wire, headers={"Content-Type": "application/json", "Authorization": "Bearer invalid"})
    assert invalid.status_code == 401
    _, reader, _ = invited(installation, role="viewer")
    assert reader.post(root, content=wire, headers={"Content-Type": "application/json"}).status_code == 403
    # The general request ceiling still applies away from these exact file-save routes.
    messages = root.removesuffix("/research-files") + "/messages"
    assert client.post(messages, content=wire, headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post(root, content=b"x" * (6 * MAX_ARTIFACT_TEXT_BYTES + 4097),
                       headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post(root, json={"path": "over.tex", "text": source + "α"}).status_code == 400
    assert client.post(root, json={"path": "over-ascii.tex", "text": "x" * (MAX_ARTIFACT_TEXT_BYTES + 1)}).status_code == 422
    begin = {"path": "sections.tex", "client_id": "stable-ui-draft"}
    draft = client.post(root + "/drafts", json=begin).json()
    path = root + "/drafts/" + draft["draft_id"]
    first = client.post(path + "/append", json={"offset": 0, "text": source}).json()
    assert first["next_offset"] == len(source)
    assert client.post(path + "/append", json={"offset": 0, "text": source}).json() == first
    assert client.post(root + "/drafts", json=begin).json() == first
    assert client.post(path + "/append", json={"offset": True, "text": "bad"}).status_code == 422
    assert client.get(path + "/content").content == source.encode()
    assert len(client.get(root).json()) == 2  # Draft is not a completed file.
    second = client.post(path + "/append", json={"offset": len(source), "text": "\\end{document}\n"}).json()
    whole = source + "\\end{document}\n"
    assert second["sha256"] == hashlib.sha256(whole.encode()).hexdigest()
    finished = client.post(path + "/finish", json={"sha256": second["sha256"]}).json()
    assert client.get(finished["download_url"]).content == whole.encode()
    assert client.get(root + "/drafts").json() == []


def test_recovery_rechecks_topic_access_and_does_not_expose_other_topics(installation):
    state, client, owner, topics, _, root, _, requests = generate_fixture_file(installation, interrupted=True)
    candidate = client.get(root + "/recovery").json()[0]
    body = {key: candidate[key] for key in ("event_sequence", "tool_index", "sha256")}
    other = root.replace(topics[1]["id"], topics[2]["id"])
    assert client.get(other + "/recovery").json() == []
    assert client.post(other + "/recovery", json=body).status_code == 404
    _, reader, _ = invited(installation, role="viewer")
    assert reader.get(root + "/recovery").status_code == 200
    assert reader.post(root + "/recovery", json=body).status_code == 403
    with state.store.transaction(write=True) as db:
        db.execute("DELETE FROM memberships WHERE organization_id=? AND user_id=?", (owner["organization_id"], owner["user_id"]))
    assert client.get(root + "/recovery").status_code in {403, 404}
    assert client.post(root + "/recovery", json=body).status_code in {403, 404}
    assert len(requests) == 1


def test_malformed_retained_calls_do_not_break_file_recovery_listing(installation):
    state, owner, topics, row, _ = source_run(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    disguised_calls = json.dumps([{"function": {"name": "artifact_write", "arguments": json.dumps({
        "path": "disguised.tex", "text": "Not an actual retained tool-call array.",
    })}}])
    with state.store.transaction(write=True) as db:
        for calls in [None, 1, "not JSON", "[]", disguised_calls, {}, [{"function": {"name": "compute_submit", "arguments": "{}"}}]]:
            state._event(db, org, row["id"], {"phase": "provider_partial_response", "response": {
                "choices": [{"message": {"tool_calls": calls}}],
            }})
    state.finish(row, "failed", error="provider_protocol", no_charge=True)
    root = f"/api/organizations/{org}/topics/{topic}/research-files"
    response = installation[2].get(root + "/recovery")
    assert response.status_code == 200 and response.json() == []


@pytest.mark.parametrize("arguments", [
    '{"path":"a.tex","text":"first","text":"second"}',
    '{"path":"../bad.tex","text":"source"}',
    '{"path":"a.tex","text":"source","command":"execute"}',
    '{"path":"a.tex","text":"\\ud800"}',
    '{"path":"a.tex","text":"\\u0000"}',
    '{"path":"a.tex","text":',
    json.dumps({"path": "a.tex", "text": "α" * (MAX_ARTIFACT_TEXT_BYTES // 2 + 1)}),
])
def test_ambiguous_unsafe_oversized_and_incomplete_recovery_arguments_are_ignored(arguments):
    assert _file_arguments({"function": {"name": "artifact_write", "arguments": arguments}}) is None


@pytest.mark.parametrize("path", ["a.tex", "a." + "b" * 198, "a" * 196 + ".tex", "dir/.tex", "dir/source"])
def test_recovery_always_proposes_a_safe_new_bounded_filename(path):
    result = _copy_path(path, 2**63 - 1, 10000)
    assert safe_path(result) and result != path and len(result) <= 200


def test_file_delivery_migration_preserves_existing_source_and_allows_resuming_new_drafts(tmp_path, monkeypatch):
    from reasoning_ledger.server import store as store_module
    from reasoning_ledger.server.research_artifact_drafts import ResearchArtifactDrafts

    migrations = store_module.MIGRATIONS
    with monkeypatch.context() as previous:
        previous.setattr(store_module, "MIGRATIONS", migrations[:-2])
        old = store_module.Store(tmp_path / "old-installation")
        owner = old.bootstrap(old._setup_token, "owner@example.org", "Owner", "a long fixture password", "Research")
        user, org = owner["user_id"], owner["organization_id"]
        project = old.create_entity(user, org, "projects", "Research")
        channel = old.create_entity(user, org, "channels", "Files", project["id"])
        topic = old.create_entity(user, org, "topics", "Documents", channel["id"], "beta")["id"]
        source = synthetic_latex()
        saved = ResearchArtifacts(old).write(user, org, topic, "existing.tex", source)
    upgraded = store_module.Store(old.data_dir)
    assert ResearchArtifacts(upgraded).content(user, org, topic, "existing.tex", saved["revision"])[0] == source.encode()
    drafts = ResearchArtifactDrafts(upgraded)
    draft = drafts.begin(user, org, topic, "new.tex", "migration-draft")
    progress = drafts.append(user, org, topic, draft["draft_id"], 0, source)
    result = drafts.finish(user, org, topic, draft["draft_id"], progress["sha256"])
    assert ResearchArtifacts(upgraded).content(user, org, topic, "new.tex", result["revision"])[0] == source.encode()
    with upgraded.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(migrations)
