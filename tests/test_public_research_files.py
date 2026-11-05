"""Large files assembled from saved sections, without resending source or calling a provider."""

import hashlib
import json

import pytest
from test_public_inference import prepare
from test_public_server import installation as installation
from test_public_server import invited

from reasoning_ledger.server import artifact_chunks
from reasoning_ledger.server.research_artifacts import MAX_ASSEMBLY_PARTS, ResearchArtifacts
from reasoning_ledger.server.research_file_limits import MAX_ARTIFACT_TEXT_BYTES
from reasoning_ledger.server.store import Store, StoreError


@pytest.fixture
def project_files(tmp_path):
    store = Store(tmp_path / "files")
    owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "a long fixture password", "Research")
    user, org = owner["user_id"], owner["organization_id"]
    topics = []
    for name in ("Research", "Elsewhere"):
        project = store.create_entity(user, org, "projects", name)
        channel = store.create_entity(user, org, "channels", "Files", project["id"])
        topics.append(store.create_entity(user, org, "topics", "Documents", channel["id"], "beta")["id"])
    return store, ResearchArtifacts(store), (user, org, topics[0]), (user, org, topics[1])


def sections(count=3):
    # Each section is close to the direct-save limit, so the whole exceeds it and is kept in blob storage.
    line = "\\textbf{αβγ 😀}: \\\\ and \"quoted\" source.\n"
    return [f"% section {index}\n" + line * ((MAX_ARTIFACT_TEXT_BYTES - 64) // len(line.encode())) for index in range(count)]


def test_assembly_joins_pinned_sections_exactly_and_repeats_return_the_same_revision(project_files):
    store, files, scope, _ = project_files
    texts = sections()
    parts = [{"path": f"paper/{index:02d}.tex", "revision": files.write(*scope, f"paper/{index:02d}.tex", text)["revision"]}
             for index, text in enumerate(texts)]
    whole = "".join(texts)
    assert len(whole.encode()) > MAX_ARTIFACT_TEXT_BYTES
    saved = files.assemble(*scope, "paper.tex", parts)
    assert saved["revision"] == 1 and saved["parts"] == 3 and saved["size_bytes"] == len(whole.encode())
    assert saved["sha256"] == hashlib.sha256(whole.encode()).hexdigest()
    assert files.content(*scope, "paper.tex", 1)[0] == whole.encode()
    assert files.assemble(*scope, "paper.tex", parts, saved["sha256"]) == saved
    # Parts are pinned: revising a section later does not change what an assembly of the old revision joins.
    revised = files.write(*scope, "paper/01.tex", "A revised middle section.\n", files.read(*scope, "paper/01.tex")["sha256"])
    assert files.assemble(*scope, "paper.tex", parts, saved["sha256"]) == saved
    updated = files.assemble(*scope, "paper.tex", [parts[0], {"path": "paper/01.tex", "revision": revised["revision"]}, parts[2]],
                             saved["sha256"])
    assert updated["revision"] == 2
    assert files.content(*scope, "paper.tex", 2)[0] == (texts[0] + "A revised middle section.\n" + texts[2]).encode()
    assert files.content(*scope, "paper.tex", 1)[0] == whole.encode()
    assert sorted(file["path"] for file in files.list(*scope)) == ["paper.tex", "paper/00.tex", "paper/01.tex", "paper/02.tex"]
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'research_artifact_draft%'").fetchone()[0] == 0


def test_an_intervening_edit_is_resolved_by_assembling_again_with_the_current_hash(project_files):
    _, files, scope, _ = project_files
    first = files.write(*scope, "intro.tex", "Introduction.\n")
    parts = [{"path": "intro.tex", "revision": first["revision"]}]
    with pytest.raises(StoreError, match="does not yet exist") as missing:
        files.assemble(*scope, "fresh.tex", parts, "0" * 64)
    assert missing.value.status == 409
    saved = files.assemble(*scope, "paper.tex", parts)
    colleague = files.write(*scope, "paper.tex", "A colleague's edit.\n", saved["sha256"])
    with pytest.raises(StoreError) as conflict:
        files.assemble(*scope, "paper.tex", parts + parts, saved["sha256"])
    assert conflict.value.status == 409
    with pytest.raises(StoreError) as unguarded:
        files.assemble(*scope, "paper.tex", parts + parts)
    assert unguarded.value.status == 409
    # Nothing is stuck: the saved sections are still there, so the same assembly succeeds with the current hash.
    again = files.assemble(*scope, "paper.tex", parts + parts, colleague["sha256"])
    assert again["revision"] == 3 and files.content(*scope, "paper.tex", 3)[0] == b"Introduction.\nIntroduction.\n"


@pytest.mark.parametrize("parts", [
    [], [{"path": "intro.tex"}], [{"path": "intro.tex", "revision": 0}], [{"path": "intro.tex", "revision": True}],
    [{"path": "../intro.tex", "revision": 1}], [{"path": "intro.tex", "revision": 1, "text": "inline"}],
    [{"path": "intro.tex", "revision": 1}] * (MAX_ASSEMBLY_PARTS + 1), {"path": "intro.tex", "revision": 1},
])
def test_assembly_rejects_malformed_parts_before_saving(project_files, parts):
    _, files, scope, _ = project_files
    files.write(*scope, "intro.tex", "Introduction.\n")
    with pytest.raises(StoreError) as invalid:
        files.assemble(*scope, "paper.tex", parts)
    assert invalid.value.status == 400
    assert [file["path"] for file in files.list(*scope)] == ["intro.tex"]


def test_assembly_reads_only_saved_revisions_of_this_project_within_the_size_allowance(project_files, monkeypatch):
    _, files, scope, elsewhere = project_files
    files.write(*elsewhere, "secret.tex", "Another project's source.\n")
    local = files.write(*scope, "intro.tex", "Introduction.\n")
    for parts in ([{"path": "secret.tex", "revision": 1}], [{"path": "intro.tex", "revision": local["revision"] + 1}]):
        with pytest.raises(StoreError) as missing:
            files.assemble(*scope, "paper.tex", parts)
        assert missing.value.status == 404
    with pytest.raises(StoreError) as unsafe:
        files.assemble(*scope, "../paper.tex", [{"path": "intro.tex", "revision": 1}])
    assert unsafe.value.status == 400
    monkeypatch.setattr(artifact_chunks, "MAX_BYTES", 20)
    with pytest.raises(StoreError) as oversized:
        files.assemble(*scope, "paper.tex", [{"path": "intro.tex", "revision": 1}] * 2)
    assert oversized.value.status == 413
    assert [file["path"] for file in files.list(*elsewhere)] == ["secret.tex"]
    assert [file["path"] for file in files.list(*scope)] == ["intro.tex"]


def test_native_assembly_attaches_only_the_assembled_file_and_records_its_parts(installation):
    from test_organization_research_workbench import source_run

    state, owner, topics, run, tools = source_run(installation)
    first = tools.invoke("artifact_write", {"path": "paper/01.tex", "text": "\\documentclass{article}\n"})["result"]
    second = tools.invoke("tool_call", {"name": "artifact_write",
                                        "arguments": {"path": "paper/02.tex", "text": "Section α and 研究.\n"}})["result"]["result"]
    notes = tools.invoke("artifact_write", {"path": "notes.md", "text": "Reviewer notes.\n"})["result"]
    parts = [{"path": "paper/01.tex", "revision": first["revision"]}, {"path": "paper/02.tex", "revision": second["revision"]}]
    saved = tools.invoke("artifact_assemble", {"path": "paper.tex", "parts": parts})["result"]
    assert saved["parts"] == 2
    state.finish(run, "succeeded", no_charge=True)
    messages = state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"])
    reply = next(message for message in messages if message.get("inference_run_id") == run["id"])
    # The sections stay in Project files; the reply lists the assembled file and the unrelated note.
    assert [(file["path"], file["revision"]) for file in reply["files"]] == [("notes.md", notes["revision"]), ("paper.tex", 1)]
    assert installation[2].get(reply["files"][1]["download_url"]).content == "\\documentclass{article}\nSection α and 研究.\n".encode()
    with state.store.transaction() as db:
        receipt = db.execute("SELECT assembled_from_json FROM research_artifact_publications WHERE artifact_id=?",
                             (saved["id"],)).fetchone()
        assert json.loads(receipt[0]) == parts
        assert db.execute("SELECT run_id FROM research_artifact_versions WHERE artifact_id=?", (saved["id"],)).fetchone()[0] == run["id"]


def test_section_files_stay_hidden_when_the_assembly_tool_receipt_is_lost(installation):
    from test_organization_research_workbench import source_run

    state, owner, topics, run, tools = source_run(installation)
    section = tools.invoke("artifact_write", {"path": "part.tex", "text": "Only section.\n"})["result"]
    files = ResearchArtifacts(state.store)
    # A crash after the save commits but before the outer tool receipt: only the publication receipt remains.
    files.assemble(owner["user_id"], owner["organization_id"], topics[1]["id"], "whole.tex",
                   [{"path": "part.tex", "revision": section["revision"]}], run_id=run["id"])
    state.finish(run, "succeeded", no_charge=True)
    reply = next(message for message in state.store.messages(owner["user_id"], owner["organization_id"], topics[1]["id"])
                 if message.get("inference_run_id") == run["id"])
    assert [file["path"] for file in reply["files"]] == ["whole.tex"]


def test_assembly_route_requires_topic_write_access_and_validates_its_body(installation):
    _, _, client, owner, topics = prepare(installation)
    root = f"/api/organizations/{owner['organization_id']}/topics/{topics[1]['id']}/research-files"
    section = client.post(root, json={"path": "intro.tex", "text": "Introduction.\n"}).json()
    body = {"path": "paper.tex", "parts": [{"path": "intro.tex", "revision": section["revision"]}] * MAX_ASSEMBLY_PARTS}
    saved = client.post(root + "/assemble", json=body)
    assert saved.status_code == 200 and saved.json()["parts"] == MAX_ASSEMBLY_PARTS
    assert client.get(saved.json()["download_url"]).content == b"Introduction.\n" * MAX_ASSEMBLY_PARTS
    assert client.post(root + "/assemble", json={**body, "expected_sha256": saved.json()["sha256"]}).json()["revision"] == 1
    for invalid in ({**body, "parts": []}, {**body, "parts": [{"path": "intro.tex", "revision": "1"}]},
                    {**body, "parts": body["parts"] + body["parts"][:1]}, {**body, "expected_sha256": "short"}):
        assert client.post(root + "/assemble", json=invalid).status_code == 422
    _, reader, _ = invited(installation, role="viewer")
    assert reader.post(root + "/assemble", json=body).status_code == 403
