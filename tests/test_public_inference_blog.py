"""Both inference engines can revise and publish authorized project blog posts.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import pytest
from test_public_bot_runtime import setup
from test_public_bots import fixture as bot_fixture
from test_public_server import installation as installation
from test_public_topic_agents import root

from reasoning_ledger.server.bot_tools import BotTools
from reasoning_ledger.server.knowledge_store import KnowledgeStore
from reasoning_ledger.server.research_tools import ResearchTools
from reasoning_ledger.server.store import StoreError


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


def test_openrouter_can_edit_published_post_with_revision_guard_and_private_audience(installation):
    inference, _, owner, topics, run, _ = root(installation)
    tools = ResearchTools(inference, run)

    def invoke(name, arguments):
        return tools.invoke(name, arguments)["result"]

    create = invoke("blog_create", {"title": "Research", "body": "First version"})
    post = create["id"]
    published = invoke("blog_publish", {"entry_id": post, "expected_revision": 1})
    assert published["state"] == "published" and published["published_revision"] == 2
    edited = invoke(
        "blog_revise", {"entry_id": post, "title": "Updated", "body": "Corrected evidence", "expected_revision": 2, "state": "published"}
    )
    assert edited["revision"] == edited["published_revision"] == 3
    assert invoke("blog_read", {"entry_id": post, "revision": 2})["body"] == "First version"
    with pytest.raises(StoreError, match="changed"):
        tools.registry.invoke("blog_revise", {"entry_id": post, "title": "Stale", "body": "Overwrite", "expected_revision": 2})
    with inference.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM research_proposals").fetchone()[0] == 0
    knowledge = KnowledgeStore(inference.store)
    draft = invoke("blog_revise", {"entry_id": post, "title": "Draft", "body": "Not published", "expected_revision": 3})
    assert draft["revision"] == 4 and draft["published_revision"] == 3
    assert knowledge.get(owner["user_id"], owner["organization_id"], post, 3)["body"] == "Corrected evidence"
    foreign = inference.store.create_entity(owner["user_id"], owner["organization_id"], "projects", "Other project")
    other = knowledge.create(
        owner["user_id"],
        owner["organization_id"],
        kind="journal",
        title="Other",
        body="Hidden",
        state="published",
        scope_kind="project",
        scope_id=foreign["id"],
        client_id="other-project-post",
    )
    with pytest.raises(StoreError, match="unavailable"):
        tools.registry.invoke("blog_publish", {"entry_id": other["id"], "expected_revision": 1})
    shared = knowledge.create(
        owner["user_id"],
        owner["organization_id"],
        kind="journal",
        title="Shared",
        body="Organization post",
        state="draft",
        scope_kind="organization",
        client_id="shared-organization-post",
    )
    assert invoke("blog_publish", {"entry_id": shared["id"], "expected_revision": 1})["published_revision"] == 2


def test_bot_published_edits_search_and_permission_revocation(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    lease = runtime.claim(bot["token"])["lease"]
    tools = BotTools(runtime)

    def invoke(name, arguments, client_id):
        return tools.invoke(bot["token"], run["id"], lease, name, arguments, client_id)

    names = {tool["name"] for tool in tools.context(bot["token"], run["id"], lease)["tools"]}
    assert {"blog_read", "blog_search", "blog_create", "blog_revise", "blog_publish"} <= names
    created = invoke("blog_create", {"title": "Bot note", "body": "Evidence"}, "create-blog")
    published = invoke("blog_publish", {"entry_id": created["id"], "expected_revision": 1}, "publish-blog")
    assert published["revision"] == published["published_revision"] == 2
    revised = invoke(
        "blog_revise",
        {"entry_id": created["id"], "expected_revision": 2, "title": "Corrected", "body": "Verified citation", "state": "published"},
        "revise-blog",
    )
    assert revised["revision"] == revised["published_revision"] == 3
    assert invoke("blog_read", {"entry_id": created["id"], "revision": 2}, "old-revision")["body"] == "Evidence"
    narrow = invoke("blog_search", {"query": "Corrected"}, "search-blog")
    assert narrow["entries"] == [] and narrow["knowledge_scope"]["effective"] == "topic"
    wider = invoke("blog_search", {"query": "Corrected", "scope": "project"}, "search-project-blog")
    assert wider["entries"][0]["id"] == created["id"] and wider["knowledge_scope"]["effective"] == "project"
    shared = KnowledgeStore(store).create(
        owner["user_id"],
        owner["organization_id"],
        kind="journal",
        title="Shared",
        body="Organization-wide evidence",
        state="draft",
        scope_kind="organization",
        client_id="shared-organization-post",
    )
    assert invoke("blog_publish", {"entry_id": shared["id"], "expected_revision": 1}, "publish-shared")["published_revision"] == 2
    foreign_topic = store.create_entity(
        owner["user_id"],
        owner["organization_id"],
        "topics",
        "Other beta",
        store.hierarchy(owner["user_id"], owner["organization_id"])["topics"][1]["channel_id"],
        "beta",
    )
    other = KnowledgeStore(store).create(
        owner["user_id"],
        owner["organization_id"],
        kind="journal",
        title="Other topic",
        body="Another topic draft",
        state="draft",
        scope_kind="topic",
        scope_id=foreign_topic["id"],
        client_id="other-topic-post",
    )
    with pytest.raises(StoreError, match="unavailable"):
        invoke("blog_read", {"entry_id": other["id"]}, "read-other-topic")
    assert other["id"] not in {post["id"] for post in invoke("blog_search", {"scope": "project"}, "search-scope")["entries"]}
    with pytest.raises(StoreError, match="changed"):
        invoke("blog_revise", {"entry_id": created["id"], "expected_revision": 2, "title": "Stale", "body": "Lost update"}, "stale-blog")
    with store.transaction(write=True) as db:
        db.execute("UPDATE topics SET knowledge_policy_json='[]' WHERE id=?", (topics[1],))
    with pytest.raises(StoreError, match="grant"):
        invoke("blog_publish", {"entry_id": created["id"], "expected_revision": 3}, "after-revoke")
