"""Exact, resumable file delivery without provider calls."""

import hashlib

import pytest
from test_public_inference import prepare
from test_public_server import installation as installation
from test_public_server import invited

from reasoning_ledger.server.research_artifact_drafts import ResearchArtifactDrafts
from reasoning_ledger.server.research_artifacts import ResearchArtifacts
from reasoning_ledger.server.store import Store, StoreError


def draft_setup(installation):
    config, state, client, owner, topics = prepare(installation)
    user, org, topic = owner["user_id"], owner["organization_id"], topics[1]["id"]
    return config, state.store, client, (user, org, topic), topics


def test_file_draft_survives_restart_and_publishes_exact_source_once(installation):
    config, store, client, scope, _ = draft_setup(installation)
    files, drafts = ResearchArtifacts(store), ResearchArtifactDrafts(store)
    source = "\\documentclass{article}\n\\begin{document}\n" + "\\textbf{αβγ 😀}: \\\\ and \"quoted\".\n" * 15000 + "\\end{document}\n"
    chunks = [source[offset:offset + 30000] for offset in range(0, len(source), 30000)]
    first = drafts.begin(*scope, "exact.tex", "stable-file-id")
    identity = first["draft_id"]
    progress = drafts.append(*scope, identity, 0, chunks[0])
    assert progress["next_offset"] == len(chunks[0]) and progress["status"] == "draft"
    assert files.list(*scope) == []
    assert drafts.append(*scope, identity, 0, chunks[0]) == progress
    drafts = ResearchArtifactDrafts(Store(config.data_dir))
    assert drafts.begin(*scope, "exact.tex", "stable-file-id") == progress
    assert drafts.content(*scope, identity)[0] == chunks[0].encode()
    offset = len(chunks[0])
    for chunk in chunks[1:]:
        progress = drafts.append(*scope, identity, offset, chunk)
        offset += len(chunk)
        assert progress["next_offset"] == offset
    boundary = drafts.read(*scope, identity, offset=29990, length=40)
    assert boundary["text"] == source[29990:30030] and boundary["next_offset"] == 30030
    checksum = hashlib.sha256(source.encode()).hexdigest()
    assert progress["sha256"] == checksum
    saved = drafts.finish(*scope, identity, checksum)
    assert saved["revision"] == 1 and saved["sha256"] == checksum
    assert client.get(saved["download_url"]).content == source.encode()
    assert drafts.list(*scope) == []
    replacement = files.write(*scope, "exact.tex", "A later independent revision.", expected_sha256=checksum)
    assert replacement["revision"] == 2
    assert drafts.finish(*scope, identity, checksum) == saved
    assert drafts.append(*scope, identity, 0, chunks[0])["result"] == saved
    assert client.get(saved["download_url"]).content == source.encode()
    assert len(files.list(*scope)) == 1


def test_file_draft_retries_reject_conflicts_and_validate_utf8_byte_limits(installation, monkeypatch):
    _, store, _, scope, _ = draft_setup(installation)
    drafts = ResearchArtifactDrafts(store)
    first = drafts.begin(*scope, "safe.tex", "stable-id")
    identity = first["draft_id"]
    with pytest.raises(StoreError, match="different content"):
        drafts.begin(*scope, "other.tex", "stable-id")
    with pytest.raises(StoreError, match="safe relative"):
        drafts.begin(*scope, "../escape.tex", "escape-id")
    with pytest.raises(StoreError, match="exact current SHA"):
        drafts.begin(*scope, "safe.tex", "invalid-sha", expected_sha256="not-a-sha")
    for text in ["\0", "\ud800", "😀" * 65537, ""]:
        with pytest.raises(StoreError):
            drafts.append(*scope, identity, 0, text)
    progress = drafts.append(*scope, identity, 0, "α")
    assert progress["total_characters"] == 1 and progress["size_bytes"] == 2
    with pytest.raises(StoreError, match="conflicts"):
        drafts.append(*scope, identity, 0, "β")
    with pytest.raises(StoreError, match="next offset changed"):
        drafts.append(*scope, identity, 2, "gap")
    with pytest.raises(StoreError, match="current checksum"):
        drafts.finish(*scope, identity, "0" * 64)
    monkeypatch.setattr("reasoning_ledger.server.research_artifact_drafts.MAX_BYTES", 3)
    with pytest.raises(StoreError, match="allowance"):
        drafts.append(*scope, identity, 1, "β")
    monkeypatch.setattr("reasoning_ledger.server.research_artifact_drafts.MAX_DRAFT_CHUNKS", 1)
    with pytest.raises(StoreError, match="allowance"):
        drafts.append(*scope, identity, 1, "a")
    assert drafts.content(*scope, identity)[0] == "α".encode()
    assert drafts.read(*scope, identity)["sha256"] == progress["sha256"]


def test_file_draft_keeps_source_when_publication_conflicts(installation):
    _, store, _, scope, _ = draft_setup(installation)
    files, drafts = ResearchArtifacts(store), ResearchArtifactDrafts(store)
    old = files.write(*scope, "shared.tex", "An earlier version.")
    identity = drafts.begin(*scope, "shared.tex", "update-file", expected_sha256=old["sha256"])["draft_id"]
    progress = drafts.append(*scope, identity, 0, "A recovered author draft.")
    current = files.write(*scope, "shared.tex", "An intervening edit.", expected_sha256=old["sha256"])
    with pytest.raises(StoreError, match="artifact changed"):
        drafts.finish(*scope, identity, progress["sha256"])
    assert files.read(*scope, "shared.tex")["sha256"] == current["sha256"]
    assert drafts.content(*scope, identity)[0] == b"A recovered author draft."
    assert drafts.list(*scope)[0]["status"] == "draft"


def test_file_draft_validates_destination_before_drafting_but_retains_resumable_sessions(installation):
    _, store, _, scope, _ = draft_setup(installation)
    files, drafts = ResearchArtifacts(store), ResearchArtifactDrafts(store)
    first = files.write(*scope, "shared.tex", "An existing saved file.")
    for base in [None, "0" * 64]:
        with pytest.raises(StoreError, match="current hash before starting"):
            drafts.begin(*scope, "shared.tex", "invalid-base", expected_sha256=base)
        assert drafts.list(*scope) == []
    with pytest.raises(StoreError, match="does not yet exist"):
        drafts.begin(*scope, "missing.tex", "missing-destination", expected_sha256=first["sha256"])
    assert drafts.list(*scope) == []
    draft = drafts.begin(*scope, "shared.tex", "valid-base", expected_sha256=first["sha256"])
    progress = drafts.append(*scope, draft["draft_id"], 0, "An unfinished replacement.")
    files.write(*scope, "shared.tex", "A subsequent edit.", expected_sha256=first["sha256"])
    # Resume returns the original draft instead of discarding work when its base is stale.
    assert drafts.begin(*scope, "shared.tex", "valid-base", expected_sha256=first["sha256"]) == progress
    assert drafts.content(*scope, draft["draft_id"])[0] == b"An unfinished replacement."


def test_file_drafts_are_owner_topic_and_current_permission_bound(installation):
    _, store, _, scope, topics = draft_setup(installation)
    drafts = ResearchArtifactDrafts(store)
    user, org, topic = scope
    identity = drafts.begin(*scope, "private-draft.tex", "private-draft")["draft_id"]
    progress = drafts.append(*scope, identity, 0, "Not yet a shared file.")
    _, _, member = invited(installation)
    member_scope = member["user_id"], org, topic
    assert drafts.list(*member_scope) == []
    for other_scope in [member_scope, (user, org, topics[2]["id"]), (user, "0" * 32, topic)]:
        for operation in [lambda other_scope=other_scope: drafts.read(*other_scope, identity),
                          lambda other_scope=other_scope: drafts.content(*other_scope, identity),
                          lambda other_scope=other_scope: drafts.append(*other_scope, identity, 21, "more"),
                          lambda other_scope=other_scope: drafts.finish(*other_scope, identity, progress["sha256"])]:
            with pytest.raises(StoreError) as denied:
                operation()
            assert denied.value.status in {403, 404}
    with store.transaction(write=True) as db:
        db.execute("UPDATE memberships SET role='viewer' WHERE user_id=? AND organization_id=?", (user, org))
    assert drafts.content(*scope, identity)[0] == b"Not yet a shared file."
    with pytest.raises(StoreError) as denied:
        drafts.append(*scope, identity, 21, "more")
    assert denied.value.status == 403
    with pytest.raises(StoreError) as denied:
        drafts.finish(*scope, identity, progress["sha256"])
    assert denied.value.status == 403
    with store.transaction(write=True) as db:
        db.execute("DELETE FROM memberships WHERE user_id=? AND organization_id=?", (user, org))
    with pytest.raises(StoreError):
        drafts.content(*scope, identity)


def test_file_draft_verifies_retained_chunk_content_before_download_or_publication(installation):
    _, store, _, scope, _ = draft_setup(installation)
    drafts = ResearchArtifactDrafts(store)
    identity = drafts.begin(*scope, "checked.tex", "checked-draft")["draft_id"]
    progress = drafts.append(*scope, identity, 0, "Exact retained source.")
    with store.transaction(write=True) as db:
        db.execute("DROP TRIGGER research_artifact_draft_chunk_update")
        db.execute("UPDATE research_artifact_draft_chunks SET text='corrupt' WHERE draft_id=?", (identity,))
    for operation in [lambda: drafts.content(*scope, identity), lambda: drafts.read(*scope, identity),
                      lambda: drafts.finish(*scope, identity, progress["sha256"])]:
        with pytest.raises(StoreError, match="retained content"):
            operation()
    assert ResearchArtifacts(store).list(*scope) == []


def test_native_research_tools_resume_and_finish_an_exact_file_draft(installation):
    from test_organization_research_workbench import source_run

    state, owner, topics, run, tools = source_run(installation)
    first = tools.invoke("artifact_begin", {"path": "native.tex", "client_id": "native-file-draft"})["result"]
    appended = tools.invoke("artifact_append", {"draft_id": first["draft_id"], "offset": 0,
                                                 "text": "\\documentclass{article}\nαβγ\n"})["result"]
    progress = tools.invoke("artifact_drafts", {})["result"]
    assert progress[0]["next_offset"] == appended["next_offset"]
    source = tools.invoke("artifact_draft_read", {"draft_id": first["draft_id"]})["result"]
    assert source["text"] == "\\documentclass{article}\nαβγ\n"
    saved = tools.invoke("artifact_finish", {"draft_id": first["draft_id"], "sha256": appended["sha256"]})["result"]
    assert saved["revision"] == 1 and saved["sha256"] == source["sha256"]
    with state.store.transaction() as db:
        assert db.execute("SELECT run_id FROM research_artifact_versions WHERE artifact_id=?", (saved["id"],)).fetchone()[0] == run["id"]


@pytest.mark.parametrize("operation", ["begin", "append", "finish"])
def test_file_draft_mutations_recheck_run_authorization_inside_the_write(installation, monkeypatch, operation):
    from test_organization_research_workbench import source_run

    state, owner, topics, run, tools = source_run(installation)
    scope = owner["user_id"], owner["organization_id"], topics[1]["id"]
    drafts = ResearchArtifactDrafts(state.store)
    first = tools.invoke("artifact_begin", {"path": "fenced.tex", "client_id": "fenced-draft"})["result"]
    progress = tools.invoke("artifact_append", {"draft_id": first["draft_id"], "offset": 0, "text": "Retained source."})["result"]
    original = tools._authorize

    def cancel_after_precheck():
        original()
        with state.store.transaction(write=True) as db:
            db.execute("UPDATE llm_runs SET cancel_requested=1 WHERE id=?", (run["id"],))

    monkeypatch.setattr(tools, "_authorize", cancel_after_precheck)
    arguments = {"begin": {"path": "late.tex", "client_id": "late-draft"},
                 "append": {"draft_id": first["draft_id"], "offset": progress["next_offset"], "text": "Late source."},
                 "finish": {"draft_id": first["draft_id"], "sha256": progress["sha256"]}}[operation]
    with pytest.raises(ValueError, match="execution no longer authorized"):
        tools.invoke("artifact_" + operation, arguments)
    assert drafts.content(*scope, first["draft_id"])[0] == b"Retained source."
    assert len(drafts.list(*scope)) == 1
    assert ResearchArtifacts(state.store).list(*scope) == []
