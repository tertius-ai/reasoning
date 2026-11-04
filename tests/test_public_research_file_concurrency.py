"""Concurrent retries exercise real persistent draft transactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from reasoning_ledger.server.research_artifact_drafts import ResearchArtifactDrafts
from reasoning_ledger.server.research_artifacts import ResearchArtifacts
from reasoning_ledger.server.store import Store, StoreError


@pytest.fixture
def drafts(tmp_path):
    store = Store(tmp_path / "files")
    owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "a long fixture password", "Research")
    user, org = owner["user_id"], owner["organization_id"]
    project = store.create_entity(user, org, "projects", "Research")
    channel = store.create_entity(user, org, "channels", "Files", project["id"])
    topic = store.create_entity(user, org, "topics", "Documents", channel["id"], "beta")["id"]
    return store, ResearchArtifactDrafts(store), (user, org, topic)


def simultaneous(count, operation):
    ready = Barrier(count)

    def invoke(index):
        ready.wait(timeout=10)
        try:
            return operation(index)
        except StoreError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(invoke, range(count)))


def test_concurrent_begin_retries_create_one_resumable_session(drafts):
    store, files, scope = drafts
    results = simultaneous(8, lambda _: files.begin(*scope, "survey.tex", "same-draft"))
    assert all(result == results[0] for result in results)
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM research_artifact_drafts").fetchone()[0] == 1
    assert len(files.list(*scope)) == 1


def test_concurrent_duplicate_chunks_append_once(drafts):
    store, files, scope = drafts
    draft = files.begin(*scope, "survey.tex", "duplicate-chunk")["draft_id"]
    source = "\\section{An exact section}\nα and 研究.\n" * 1000
    results = simultaneous(8, lambda _: files.append(*scope, draft, 0, source))
    assert all(result == results[0] for result in results)
    assert files.content(*scope, draft)[0] == source.encode()
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM research_artifact_draft_chunks WHERE draft_id=?", (draft,)).fetchone()[0] == 1


def test_concurrent_conflicting_chunks_preserve_exactly_one_source(drafts):
    store, files, scope = drafts
    draft = files.begin(*scope, "survey.tex", "conflicting-chunk")["draft_id"]
    sources = ["An α version.", "A β version."]
    results = simultaneous(2, lambda index: files.append(*scope, draft, 0, sources[index]))
    failures = [result for result in results if isinstance(result, StoreError)]
    assert len(failures) == 1 and failures[0].status == 409
    winner = next(index for index, result in enumerate(results) if not isinstance(result, StoreError))
    assert files.content(*scope, draft)[0] == sources[winner].encode()
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM research_artifact_draft_chunks WHERE draft_id=?", (draft,)).fetchone()[0] == 1


def test_concurrent_finishes_publish_one_revision_and_retries_keep_its_identity(drafts):
    store, files, scope = drafts
    draft = files.begin(*scope, "survey.tex", "finish-retry")["draft_id"]
    source = "\\documentclass{article}\nA saved α source.\n"
    progress = files.append(*scope, draft, 0, source)
    results = simultaneous(8, lambda _: files.finish(*scope, draft, progress["sha256"]))
    assert all(result == results[0] for result in results)
    saved = results[0]
    assert saved["revision"] == 1
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM research_artifact_versions WHERE artifact_id=?", (saved["id"],)).fetchone()[0] == 1
    artifacts = ResearchArtifacts(store)
    later = artifacts.write(*scope, "survey.tex", "An independent subsequent edit.", expected_sha256=saved["sha256"])
    assert later["revision"] == 2
    retries = simultaneous(8, lambda _: files.finish(*scope, draft, progress["sha256"]))
    assert all(result == saved for result in retries)
    assert artifacts.content(*scope, "survey.tex", saved["revision"])[0] == source.encode()
