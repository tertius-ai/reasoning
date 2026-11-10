"""Concurrent retries exercise real persistent assembly transactions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from reasoning_ledger.server.research_artifacts import ResearchArtifacts
from reasoning_ledger.server.store import Store, StoreError


@pytest.fixture
def files(tmp_path):
    store = Store(tmp_path / "files")
    owner = store.bootstrap(store._setup_token, "owner@example.org", "Owner", "a long fixture password", "Research")
    user, org = owner["user_id"], owner["organization_id"]
    project = store.create_entity(user, org, "projects", "Research")
    channel = store.create_entity(user, org, "channels", "Files", project["id"])
    topic = store.create_entity(user, org, "topics", "Documents", channel["id"], "beta")["id"]
    return store, ResearchArtifacts(store), (user, org, topic)


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


def test_concurrent_identical_assemblies_publish_one_revision(files):
    store, artifacts, scope = files
    parts = [{"path": f"part-{index}.tex", "revision": artifacts.write(*scope, f"part-{index}.tex", f"Section {index} α.\n")["revision"]}
             for index in range(4)]
    results = simultaneous(8, lambda _: artifacts.assemble(*scope, "survey.tex", parts))
    assert all(result == results[0] for result in results)
    saved = results[0]
    assert saved["revision"] == 1
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM research_artifact_versions WHERE artifact_id=?", (saved["id"],)).fetchone()[0] == 1
    assert artifacts.content(*scope, "survey.tex", 1)[0] == "".join(f"Section {index} α.\n" for index in range(4)).encode()


def test_concurrent_conflicting_assemblies_keep_exactly_one_new_revision(files):
    store, artifacts, scope = files
    parts = [{"path": f"part-{index}.tex", "revision": artifacts.write(*scope, f"part-{index}.tex", f"Section {index}.\n")["revision"]}
             for index in range(9)]
    base = artifacts.assemble(*scope, "survey.tex", parts[:1])
    # Each contender joins a different prefix (two to nine sections). Identical assemblies share one revision by design,
    # so two contenders with the same text would both succeed whenever either of them won.
    requests = [parts[:index + 2] for index in range(8)]
    assert len({len(request) for request in requests}) == len(requests)
    results = simultaneous(8, lambda index: artifacts.assemble(*scope, "survey.tex", requests[index], base["sha256"]))
    saved = [(index, result) for index, result in enumerate(results) if not isinstance(result, StoreError)]
    assert len(saved) == 1 and saved[0][1]["revision"] == 2
    assert all(result.status == 409 for result in results if isinstance(result, StoreError))
    winner, head = saved[0]
    assert artifacts.content(*scope, "survey.tex", 2)[0] == "".join(f"Section {index}.\n" for index in range(winner + 2)).encode()
    # Replaying the winner with its stale hash returns the same revision; a loser's replay still conflicts.
    assert artifacts.assemble(*scope, "survey.tex", requests[winner], base["sha256"]) == head
    with pytest.raises(StoreError) as conflict:
        artifacts.assemble(*scope, "survey.tex", requests[(winner + 1) % len(requests)], base["sha256"])
    assert conflict.value.status == 409
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM research_artifact_versions WHERE artifact_id=?", (head["id"],)).fetchone()[0] == 2
