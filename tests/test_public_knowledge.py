"""Versioned organization knowledge, scoped beta context, presence, and bot access."""

import pytest
from fastapi.testclient import TestClient

from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.knowledge_store import topic_knowledge_context

PUBLIC_URL = "https://chat.example.org"
PASSWORD = "a very long test password"


@pytest.mark.parametrize("state", ["draft", "published"])
def test_blog_deletion_hides_all_surfaces_and_retains_history(installation, state):
    from types import SimpleNamespace

    from reasoning_ledger.server.bot_store import BotStore
    from reasoning_ledger.server.knowledge_store import KnowledgeStore
    from reasoning_ledger.server.openrouter import Quote
    from reasoning_ledger.server.research_context import ResearchContext
    from reasoning_ledger.server.store import StoreError
    from reasoning_ledger.server.work_store import WorkStore
    app, client, owner, root, project, beta, _ = installation
    original = {"client_id": "delete-blog-create-001", "kind": "journal", "title": "A hidden lemma",
                "body": "Unique retained blog evidence", "state": state, "scope_kind": "project", "scope_id": project["id"]}
    post = client.post(root + "/knowledge", json=original).json()
    bots = BotStore(app.state.store)
    bot = bots.create(owner["user_id"], owner["organization_id"], "Blog reader", [beta["id"]], ["knowledge:read", "knowledge:write"])
    if state == "published":
        changed = {"client_id": "delete-blog-edit-001", "kind": "journal", "title": "A revised hidden lemma",
                   "body": "Draft pending publication", "state": "draft", "expected_revision": 1}
        assert client.put(root + "/knowledge/" + post["id"], json=changed).status_code == 200
        post = client.get(root + "/knowledge/" + post["id"]).json()
    body = {"client_id": "delete-blog-request-001", "expected_revision": post["head_revision"]}
    path = root + "/blog/" + post["id"]
    response = client.request("DELETE", path, json=body)
    assert response.status_code == 200, response.text
    assert client.request("DELETE", path, json=body).json() == response.json()
    assert client.request("DELETE", path, json={**body, "client_id": "delete-blog-repeat-001"}).json() == response.json()
    assert client.request("DELETE", path, json={**body, "expected_revision": 99}).status_code == 409
    for suffix in ("", "?revision=1"):
        assert client.get(root + "/knowledge/" + post["id"] + suffix).status_code == 404
    assert client.get(root + "/knowledge?kind=journal&q=lemma").json() == []
    assert client.get(root + "/blog").json()["posts"] == []
    for suffix in ("", ".md"):
        assert client.get(f"/blog/{owner['organization_id']}/{post['id']}" + suffix).status_code == 404
    assert "hidden lemma" not in client.get(f"/blog/{owner['organization_id']}").text
    assert client.post(root + "/knowledge", json=original).status_code == 404
    assert client.put(root + "/knowledge/" + post["id"], json={"client_id": "delete-blog-restore-001",
        "kind": "journal", "title": "Resurrected", "body": "Must not return", "state": "published",
        "expected_revision": post["head_revision"]}).status_code == 404
    knowledge = KnowledgeStore(app.state.store)
    assert knowledge.list_bot(bots, bot["token"], kind="journal") == []
    with pytest.raises(StoreError, match="blog post unavailable"):
        knowledge.get_bot(bots, bot["token"], post["id"], 1)
    with pytest.raises(StoreError, match="blog post unavailable"):
        knowledge.write_bot(bots, bot["token"], entry_id=post["id"], client_id="deleted-bot-write-001",
            kind="journal", title="Must not revive", body="Resurrection", state="draft", expected_revision=post["head_revision"])
    with app.state.store.transaction() as db:
        assert not topic_knowledge_context(db, owner["organization_id"], beta["id"])
        assert KnowledgeStore._listing(db, owner["organization_id"], kind="journal", published_only=True) == []
        assert db.execute("SELECT count(*) FROM knowledge_versions WHERE entry_id=?", (post["id"],)).fetchone()[0] == post["head_revision"]
        assert db.execute("SELECT deleted_by FROM blog_deletions WHERE entry_id=?", (post["id"],)).fetchone()[0] == owner["user_id"]
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='blog.delete' AND target_id=?", (post["id"],)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM blog_publications WHERE entry_id=?", (post["id"],)).fetchone()[0] == (state == "published")
        messages, _ = ResearchContext(app.state.store).compile(db, owner["organization_id"], beta["id"], 0,
            [Quote("fixture/math", "0", "0", "0", 200_000, 8192)], user=owner["user_id"])
        assert "hidden lemma" not in str(messages) and "Unique retained blog evidence" not in str(messages)
        with pytest.raises(StoreError, match="input version unavailable"):
            WorkStore._input(db, owner["organization_id"], project["id"],
                SimpleNamespace(kind="knowledge", id=post["id"], revision=1, sha256="unreachable"))


def test_blog_deletion_requires_current_revision_permissions_and_exact_request(installation):
    app, client, owner, root, project, _, _ = installation
    private = client.post(root + "/projects", json={"name": "Restricted", "visibility": "private", "owner_id": owner["user_id"]}).json()
    values = {"client_id": "delete-blog-private-create", "kind": "journal", "title": "Restricted result",
              "body": "Restricted body", "state": "published", "scope_kind": "project", "scope_id": private["id"]}
    post = client.post(root + "/knowledge", json=values).json()
    path = root + "/blog/" + post["id"]
    body = {"client_id": "delete-blog-private-request", "expected_revision": 1}
    for role in ("viewer", "member"):
        invitation = client.post(root + "/invitations", json={"email": role + "@example.org", "role": role}).json()
        actor = TestClient(app, base_url=PUBLIC_URL)
        session = actor.post("/api/invitations/accept", json={"token": invitation["token"], "name": role,
            "password": PASSWORD, "transport": "bearer"}).json()
        actor.headers["Authorization"] = "Bearer " + session["access_token"]
        assert actor.request("DELETE", path, json=body).status_code in {403, 404}
    assert TestClient(app, base_url=PUBLIC_URL).request("DELETE", path, json=body).status_code == 401
    assert client.request("DELETE", path, json={**body, "expected_revision": 2}).status_code == 409
    assert client.request("DELETE", path, json={**body, "expected_revision": True}).status_code == 422
    assert client.request("DELETE", path, json={**body, "client_id": "short"}).status_code == 400
    assert client.request("DELETE", path, json=body).status_code == 200
    other = client.post(root + "/knowledge", json={**values, "client_id": "delete-blog-other-create", "scope_id": project["id"]}).json()
    assert client.request("DELETE", root + "/blog/" + other["id"], json=body).status_code == 409
    ledger = client.post(root + "/knowledge", json={**values, "client_id": "delete-blog-ledger-create",
        "kind": "ledger", "state": "note"}).json()
    assert client.request("DELETE", root + "/blog/" + ledger["id"],
        json={**body, "client_id": "delete-blog-ledger-request"}).status_code == 400


def test_static_blog_math_uses_the_built_dependency_styles(tmp_path):
    import json

    from reasoning_ledger.server.research_blog import page
    manifest = tmp_path / ".vite"
    manifest.mkdir()
    (manifest / "manifest.json").write_text(json.dumps({"src/blogMathBootstrap.ts": {"imports": ["_shared.js"]},
        "_shared.js": {"css": ["assets/shared-katex.css"], "imports": ["_shared.js"]}}))
    (tmp_path / "assets").mkdir()
    for name in ("blogMathBootstrap-AbCd1234.js", "blogTheme-EfGh5678.js"):
        (tmp_path / "assets" / name).write_text("")
    html = page("Math", "$x$", "org", tmp_path, math=True).body.decode()
    assert "href='/assets/shared-katex.css'" in html
    assert "src='/assets/blogMathBootstrap-AbCd1234.js'" in html and "src='/assets/blogTheme-EfGh5678.js'" in html
    assert "markdownMath-" not in html
    plain = page("Index", "<h2>Titles</h2>", "org", tmp_path).body.decode()
    assert "shared-katex.css" not in plain and "blogMathBootstrap" not in plain
    assert "src='/assets/blogTheme-EfGh5678.js'" in plain, "every page keeps the reader's explicit color mode"


def test_static_blog_detects_math_outside_code():
    from markdown_it import MarkdownIt
    from mdit_py_plugins.dollarmath import dollarmath_plugin
    from mdit_py_plugins.texmath import texmath_plugin

    from reasoning_ledger.server.research_blog import MATH_DELIMITERS, has_math
    renderer = MarkdownIt("commonmark", {"html": False}).enable("table")
    renderer.use(texmath_plugin, delimiters="brackets").use(dollarmath_plugin, allow_labels=False, double_inline=True)
    for body in ("Inline $x^2$.", "$$\\int_0^1 x\\,dx$$", "Bracketed \\(a+b\\).", "| a |\n|---|\n| $y$ |"):
        assert has_math(renderer.parse(body)), body
    for body in ("Prices in `$5` and", "```sh\n$ ls\n```", "Plain **text**."):
        assert not has_math(renderer.parse(body)), body
    assert MATH_DELIMITERS.search("The $p$-adic case") and not MATH_DELIMITERS.search("Weekly notes")


def test_blog_publication_snapshot_draft_edit_and_explicit_unpublish(installation):
    app, client, owner, root, project, beta, _ = installation
    request = {"client_id": "blog-first-request-001", "kind": "journal", "title": "First result",
               "body": "## Result\nA **careful** argument.\n<script>alert(1)</script>\n[bad](javascript:alert(1))\n"
                       r"$x \in \{0,1\}$ and \(a*b*\) and `$not math$`",
               "state": "published", "scope_kind": "project", "scope_id": project["id"]}
    created = client.post(root + "/knowledge", json=request)
    assert created.status_code == 201, created.text
    post = created.json()
    assert client.post(root + "/knowledge", json=request).json()["revision"] == 1
    url = f"/blog/{owner['organization_id']}/{post['id']}"
    html = client.get(url)
    assert html.status_code == 200 and "<strong>careful</strong>" in html.text
    assert "<script>alert" not in html.text and 'href="javascript:' not in html.text
    assert r"$x \in \{0,1\}$" in html.text and "$a*b*$" in html.text
    assert "<code>$not math$</code>" in html.text
    assert client.get(url + ".md").text == request["body"]
    index = client.get(f"/blog/{owner['organization_id']}")
    assert index.status_code == 200
    assert "class='research-blog'" in index.text and "class='blog-entry'" in index.text
    assert f"/organization.html?organization={owner['organization_id']}&amp;view=journal" in index.text
    assert "Edit posts" in index.text and "name='project'" in index.text
    assert "blog-shell-header" in index.text and "target='_blank'" not in index.text
    assert "First result" in index.text and "careful" not in index.text
    assert "class='blog-tag'>Algebra" in html.text
    style = client.get("/blog.css")
    assert style.status_code == 200 and style.headers["content-type"].startswith("text/css")
    assert ".blog-editor" in style.text and "@media (max-width: 600px)" in style.text
    assert "prefers-color-scheme: light" in style.text and "[data-theme=light]" in style.text
    assert client.get(root + "/blog").json()["posts"][0]["project_name"] == "Algebra"
    edited = {"client_id": "blog-edit-request-001", "kind": "journal", "title": "Unpublished edit", "body": "Draft only text",
              "state": "draft", "expected_revision": 1}
    assert client.put(root + "/knowledge/" + post["id"], json=edited).json()["revision"] == 2
    assert "First result" in client.get(url).text and "Draft only text" not in client.get(url).text
    with app.state.store.transaction() as db:
        context = topic_knowledge_context(db, owner["organization_id"], beta["id"])
        assert "careful" in context and "Draft only text" not in context
    listing = app.state.store
    from reasoning_ledger.server.knowledge_store import KnowledgeStore
    published_rows = KnowledgeStore(listing).list(owner["user_id"], owner["organization_id"], kind="journal", published_only=True)
    assert published_rows[0]["title"] == "First result"
    published = {**edited, "client_id": "blog-publish-request-001", "state": "published", "expected_revision": 2}
    assert client.put(root + "/knowledge/" + post["id"], json=published).json()["revision"] == 3
    assert "Draft only text" in client.get(url).text
    unpublish = {**edited, "client_id": "blog-unpublish-request-001", "expected_revision": 3, "unpublish": True}
    assert client.put(root + "/knowledge/" + post["id"], json=unpublish).json()["revision"] == 4
    assert client.put(root + "/knowledge/" + post["id"], json=unpublish).json()["revision"] == 4
    assert client.get(url).status_code == client.get(url + ".md").status_code == 404
    assert client.get(root + "/blog").json()["posts"] == []
    assert client.get(root + "/knowledge/" + post["id"] + "?revision=1").json()["body"] == request["body"]
    other = TestClient(app, base_url=PUBLIC_URL)
    assert other.get(url).status_code == 401


def test_blog_pages_filter_projects_and_permissions_before_paginating(installation):
    app, client, owner, root, project, _, _ = installation
    created_project = client.post(root + "/projects", json={"name": "Private results", "visibility": "private",
                                                           "owner_id": owner["user_id"]})
    assert created_project.status_code == 201, created_project.text
    other_project = created_project.json()
    private = client.post(root + "/knowledge", json={"client_id": "private-blog-request-001", "kind": "journal",
        "title": "Private result", "body": "Restricted research", "state": "published", "scope_kind": "project",
        "scope_id": other_project["id"]}).json()
    identities = set()
    for index in range(51):
        response = client.post(root + "/knowledge", json={"client_id": f"blog-pagination-{index:03d}", "kind": "journal",
            "title": f"Result {index}", "body": "Published text", "state": "published", "scope_kind": "project", "scope_id": project["id"]})
        assert response.status_code == 201, response.text
        identities.add(response.json()["id"])
    first = client.get(root + "/blog", params={"project": project["id"]}).json()
    assert len(first["posts"]) == 50 and first["next_before"]
    second = client.get(root + "/blog", params={"project": project["id"], "before": first["next_before"]}).json()
    assert len(second["posts"]) == 1 and second["next_before"] is None
    assert {entry["id"] for entry in first["posts"] + second["posts"]} == identities
    static = client.get(f"/blog/{owner['organization_id']}", params={"project": project["id"]})
    assert static.status_code == 200 and "Private result</a>" not in static.text
    assert static.text.count("class='blog-entry'") == 50
    assert f"value='{project['id']}' selected" in static.text
    assert "Older posts" in static.text and f"project={project['id']}" in static.text
    older = client.get(f"/blog/{owner['organization_id']}", params={"project": project["id"], "before": first["next_before"]})
    assert older.text.count("class='blog-entry'") == 1 and "Newest" in older.text
    assert client.get(root + "/blog?before=invalid").status_code == 400
    invitation = client.post(root + "/invitations", json={"email": "member@example.org"}).json()
    accepted = TestClient(app, base_url=PUBLIC_URL).post("/api/invitations/accept", json={"token": invitation["token"], "name": "Member",
        "password": PASSWORD, "transport": "bearer"})
    assert accepted.status_code == 200, accepted.text
    member = accepted.json()
    reader = TestClient(app, base_url=PUBLIC_URL, headers={"Authorization": "Bearer " + member["access_token"]})
    visible = reader.get(root + "/blog").json()
    assert len(visible["posts"]) == 50
    assert private["id"] not in {entry["id"] for entry in visible["posts"]}
    static = reader.get(f"/blog/{owner['organization_id']}")
    assert static.status_code == 200 and "Private results" not in static.text and "Private result</a>" not in static.text
    assert reader.get(f"/blog/{owner['organization_id']}", params={"project": other_project["id"]}).status_code in {403, 404}
    assert reader.get(root + "/blog", params={"project": other_project["id"]}).status_code in {403, 404}
    assert reader.get(f"/blog/{owner['organization_id']}/{private['id']}").status_code in {403, 404}
    assert reader.get(f"/blog/{owner['organization_id']}/{private['id']}.md").status_code in {403, 404}


@pytest.fixture
def installation(tmp_path):
    config = Config(tmp_path / "state", PUBLIC_URL)
    app = create_app(config)
    client = TestClient(app, base_url=PUBLIC_URL)
    owner = client.post(
        "/api/setup",
        json={
            "setup_token": (config.data_dir / "setup-token").read_text(),
            "email": "owner@example.org",
            "name": "Owner",
            "password": PASSWORD,
            "organization_name": "Mathematics",
            "transport": "bearer",
        },
    ).json()
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    root = f"/api/organizations/{owner['organization_id']}"
    project = client.post(root + "/projects", json={"name": "Algebra"}).json()
    channel = client.post(root + "/channels", json={"name": "research", "project_id": project["id"]}).json()
    beta = client.post(root + "/topics", json={"name": "Agents", "kind": "beta", "channel_id": channel["id"]}).json()
    gamma = client.post(root + "/topics", json={"name": "Methods", "kind": "gamma", "channel_id": channel["id"]}).json()
    return app, client, owner, root, project, beta, gamma


def test_people_create_scope_and_revise_shared_knowledge(installation):
    app, client, owner, root, project, beta, _ = installation
    hierarchy = client.get(root + "/hierarchy").json()
    topic = next(item for item in hierarchy["topics"] if item["id"] == beta["id"])
    assert topic["knowledge_tools"] == ["bibliography", "journal", "ledger", "workflows"]
    assert (
        client.post(
            root + "/topics",
            json={
                "name": "Chat only",
                "kind": "beta",
                "channel_id": beta["channel_id"],
                "knowledge_tools": [],
            },
        ).json()["knowledge_tools"]
        == []
    )
    assert client.post(
        root + "/topics",
        json={"name": "Invalid tools", "kind": "alpha", "channel_id": beta["channel_id"], "knowledge_tools": ["ledger"]},
    ).status_code == 400

    created = client.post(
        root + "/knowledge",
        json={
            "client_id": "journal-create-001",
            "kind": "journal",
            "title": "Private note",
            "body": "A draft does not enter beta context.",
            "state": "draft",
            "scope_kind": "organization",
        },
    )
    assert created.status_code == 201, created.text
    entry = created.json()
    assert entry["revision"] == 1 and entry["author_name"] == "Owner"
    replay = client.post(
        root + "/knowledge",
        json={
            "client_id": "journal-create-001",
            "kind": "journal",
            "title": "Private note",
            "body": "A draft does not enter beta context.",
            "state": "draft",
            "scope_kind": "organization",
        },
    )
    assert replay.json()["id"] == entry["id"]
    changed_replay = client.post(
        root + "/knowledge",
        json={
            "client_id": "journal-create-001",
            "kind": "journal",
            "title": "Changed replay",
            "body": "A stable request identifier cannot change its input.",
            "state": "draft",
            "scope_kind": "organization",
        },
    )
    assert changed_replay.status_code == 409
    revised = client.put(
        root + "/knowledge/" + entry["id"],
        json={
            "client_id": "journal-revise-001",
            "kind": "journal",
            "title": "Published note",
            "body": "Published journal material is available.",
            "state": "published",
            "expected_revision": 1,
        },
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["revision"] == 2
    stale = client.put(
        root + "/knowledge/" + entry["id"],
        json={
            "client_id": "journal-revise-stale-001",
            "kind": "journal",
            "title": "Stale revision",
            "body": "Optimistic updates cannot replace a newer head.",
            "state": "published",
            "expected_revision": 1,
        },
    )
    assert stale.status_code == 409
    assert client.get(root + "/knowledge/" + entry["id"] + "?revision=1").json()["title"] == "Private note"

    ledger = client.post(
        root + "/knowledge",
        json={
            "client_id": "ledger-create-001",
            "kind": "ledger",
            "title": "Local claim",
            "body": "The project-scoped statement.",
            "state": "conjecture",
            "scope_kind": "project",
            "scope_id": project["id"],
        },
    )
    assert ledger.status_code == 201, ledger.text
    assert {item["id"] for item in client.get(root + "/knowledge?kind=ledger&q=claim").json()} == {ledger.json()["id"]}
    with app.state.store.transaction() as db:
        context = topic_knowledge_context(db, owner["organization_id"], beta["id"])
    assert "untrusted reference data" in context
    assert "Published journal material is available." in context
    assert "A draft does not enter beta context." not in context
    assert "The project-scoped statement." in context


def test_presence_includes_active_people_and_bots_can_publish(installation):
    _, client, owner, root, _, beta, _ = installation
    presence = client.get(root + "/presence")
    assert presence.status_code == 200
    assert presence.json()["humans"][0]["online"] is True

    created = client.post(
        root + "/bots",
        json={
            "name": "Local Codex",
            "topic_ids": [beta["id"]],
            "scopes": ["messages:read", "messages:write", "knowledge:read", "knowledge:write"],
            "lifetime_seconds": 3600,
        },
    )
    assert created.status_code == 201, created.text
    credential = created.json()["token"]
    bot = TestClient(client.app, base_url=PUBLIC_URL, headers={"Authorization": "Bearer " + credential})
    assert bot.get("/api/bot/knowledge").json() == []
    posted = bot.post(
        "/api/bot/knowledge",
        json={
            "client_id": "bot-journal-write-001",
            "kind": "journal",
            "title": "Bot report",
            "body": "A machine-authored research update.",
            "state": "published",
            "scope_kind": "organization",
        },
    )
    assert posted.status_code == 201, posted.text
    assert posted.json()["created_by"] == created.json()["id"]
    assert posted.json()["created_by_kind"] == "bot" and posted.json()["author_name"] == "Local Codex"
    assert client.get(root + "/presence").json()["bots"][0]["online"] is True
    assert client.get(root + "/knowledge?kind=journal").json()[0]["title"] == "Bot report"

    read_only = client.post(
        root + "/bots",
        json={
            "name": "Reader",
            "topic_ids": [beta["id"]],
            "scopes": ["messages:read", "knowledge:read"],
            "lifetime_seconds": 3600,
        },
    ).json()
    reader = TestClient(client.app, base_url=PUBLIC_URL, headers={"Authorization": "Bearer " + read_only["token"]})
    denied = reader.post(
        "/api/bot/knowledge",
        json={
            "client_id": "read-only-write-001",
            "kind": "ledger",
            "title": "Denied",
            "body": "A read-only bot cannot publish.",
            "state": "note",
            "scope_kind": "organization",
        },
    )
    assert denied.status_code == 403
