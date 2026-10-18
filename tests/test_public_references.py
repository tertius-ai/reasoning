"""Message links and topic/project references resolve only through the per-user access checks."""

from test_public_server import installation as installation
from test_public_server import invited, structure


def private_structure(client, org, owner_id):
    root = f"/api/organizations/{org}"
    project = client.post(root + "/projects", json={"name": "Hidden program", "visibility": "private",
                                                    "owner_id": owner_id, "member_ids": [owner_id]})
    assert project.status_code == 201, project.text
    channel = client.post(root + "/channels", json={"name": "secret", "project_id": project.json()["id"]}).json()
    topic = client.post(root + "/topics", json={"name": "Hidden lemma", "kind": "alpha", "channel_id": channel["id"]}).json()
    return project.json(), channel, topic


def references(client, org, *, topics=(), projects=()):
    return client.get(f"/api/organizations/{org}/references",
                      params={"topics": ",".join(topics), "projects": ",".join(projects)})


def test_references_resolve_current_names_only_for_accessible_targets(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    project, channel, topics = structure(client, org)
    hidden_project, _, hidden_topic = private_structure(client, org, owner["user_id"])
    readers = {"owner": client}
    for role in ("admin", "member", "viewer"):
        readers[role] = invited(installation, role=role, email=role + "@example.org")[1]
    _, insider, insider_account = invited(installation, role="member", email="insider@example.org")
    readers["project member"] = insider
    added = client.put(f"/api/organizations/{org}/projects/{hidden_project['id']}/members/{insider_account['user_id']}",
                       json={"role": "member"})
    assert added.status_code == 204, added.text
    wanted_topics, wanted_projects = [topics[0]["id"], hidden_topic["id"]], [project["id"], hidden_project["id"]]
    for name, reader in readers.items():
        result = references(reader, org, topics=wanted_topics, projects=wanted_projects)
        assert result.status_code == 200, (name, result.text)
        resolved = result.json()
        assert resolved["topics"][topics[0]["id"]] == {
            "id": topics[0]["id"], "name": "alpha", "kind": "alpha", "channel_id": channel["id"], "channel_name": "research",
            "project_id": project["id"], "project_name": "Algebra"}
        assert resolved["projects"][project["id"]] == {"id": project["id"], "name": "Algebra"}
        # Resolution never reveals more than the reader's own hierarchy.
        tree = reader.get(f"/api/organizations/{org}/hierarchy").json()
        visible_topics = {item["id"] for item in tree["topics"]}
        visible_projects = {item["id"] for item in tree["projects"]}
        for target in wanted_topics:
            assert (resolved["topics"][target] is not None) == (target in visible_topics), (name, target)
        for target in wanted_projects:
            assert (resolved["projects"][target] is not None) == (target in visible_projects), (name, target)
    for name in ("member", "viewer"):
        resolved = references(readers[name], org, topics=[hidden_topic["id"]], projects=[hidden_project["id"]]).json()
        assert resolved == {"topics": {hidden_topic["id"]: None}, "projects": {hidden_project["id"]: None}}, name
        assert "Hidden" not in references(readers[name], org, topics=[hidden_topic["id"]]).text
    insider_view = references(insider, org, topics=[hidden_topic["id"]], projects=[hidden_project["id"]]).json()
    assert insider_view["topics"][hidden_topic["id"]]["name"] == "Hidden lemma"
    assert insider_view["projects"][hidden_project["id"]]["name"] == "Hidden program"


def test_references_follow_renames_deletions_and_bounds(installation):
    _, store, client, owner = installation
    org = owner["organization_id"]
    root = f"/api/organizations/{org}"
    project, _, topics = structure(client, org)
    assert client.patch(root + "/topics/" + topics[0]["id"], json={"name": "Renamed"}).status_code == 200
    assert client.patch(root + "/projects/" + project["id"], json={"name": "Geometry"}).status_code == 200
    resolved = references(client, org, topics=[topics[0]["id"]], projects=[project["id"]]).json()
    assert resolved["topics"][topics[0]["id"]]["name"] == "Renamed"
    assert resolved["topics"][topics[0]["id"]]["project_name"] == "Geometry"
    assert resolved["projects"][project["id"]]["name"] == "Geometry"
    plan = client.get(root + "/topics/" + topics[1]["id"] + "/deletion").json()
    deleted = client.request("DELETE", root + "/topics/" + topics[1]["id"],
                             json={"confirmation": topics[1]["name"], "plan_token": plan["plan_token"]})
    assert deleted.status_code == 204, deleted.text
    absent = "0" * 32
    assert references(client, org, topics=[topics[1]["id"], absent], projects=[absent]).json() == {
        "topics": {topics[1]["id"]: None, absent: None}, "projects": {absent: None}}
    assert references(client, org, topics=["not-an-id"]).status_code == 400
    many = [f"{index:032x}" for index in range(101)]
    assert references(client, org, topics=many[:60], projects=many[60:]).status_code == 400
    assert references(client, org, topics=many[:100]).status_code == 200
    assert references(client, org).json() == {"topics": {}, "projects": {}}
    _, departed, account = invited(installation, email="departed@example.org")
    assert references(departed, org, topics=[topics[0]["id"]]).json()["topics"][topics[0]["id"]]["name"] == "Renamed"
    store.change_member(owner["user_id"], org, account["user_id"], None)
    removed = references(departed, org, topics=[topics[0]["id"]])
    assert removed.status_code in (401, 404) and "Renamed" not in removed.text


def test_message_context_centers_long_topics_and_hides_inaccessible_messages(installation):
    _, store, client, owner = installation
    org, user = owner["organization_id"], owner["user_id"]
    _, _, topics = structure(client, org)
    topic = topics[0]["id"]
    posted = [store.post_message(user, org, topic, f"Message {index}", f"long-{index}") for index in range(260)]
    store.post_message(user, org, topics[1]["id"], "Elsewhere", "elsewhere")
    root = f"/api/organizations/{org}/topics/{topic}/messages/"

    def window(message, **params):
        response = client.get(root + message["id"] + "/context", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    middle = window(posted[120])
    contents = [item["content"] for item in middle["messages"]]
    assert len(contents) == 100 and contents.index("Message 120") == 49 and middle["older"] is True
    assert contents == [f"Message {index}" for index in range(71, 171)]
    assert [item["sequence"] for item in middle["messages"]] == sorted(item["sequence"] for item in middle["messages"])
    latest = window(posted[258])
    assert [item["content"] for item in latest["messages"]][-1] == "Message 259" and len(latest["messages"]) == 100
    assert latest["older"] is True
    first = window(posted[2], limit=10)
    assert [item["content"] for item in first["messages"]] == [f"Message {index}" for index in range(0, 10)]
    assert first["older"] is False
    edited = client.patch(root + posted[120]["id"], json={"content": "Message 120, revised", "revision": 0})
    assert edited.status_code == 200, edited.text
    assert "Message 120, revised" in [item["content"] for item in window(posted[120])["messages"]]
    assert client.get(f"/api/organizations/{org}/topics/{topics[1]['id']}/messages/{posted[0]['id']}/context").status_code == 404
    assert client.get(root + "0" * 32 + "/context").status_code == 404
    assert client.get(root + posted[0]["id"] + "/context", params={"limit": 1}).status_code == 422
    _, _, hidden_topic = private_structure(client, org, user)
    secret = store.post_message(user, org, hidden_topic["id"], "Private detail", "private-detail")
    _, member, _ = invited(installation)
    hidden = member.get(f"/api/organizations/{org}/topics/{hidden_topic['id']}/messages/{secret['id']}/context")
    assert hidden.status_code == 404 and "Private detail" not in hidden.text
    assert member.get(root + posted[120]["id"] + "/context").status_code == 200


def test_published_blog_renders_references_for_each_reader(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    project, _, topics = structure(client, org)
    hidden_project, _, hidden_topic = private_structure(client, org, owner["user_id"])
    body = f"See #[topic:{topics[0]['id']}], #[project:{hidden_project['id']}] and `#[topic:{hidden_topic['id']}]` " \
           f"plus #[topic:{hidden_topic['id']}]."
    post = client.post(f"/api/organizations/{org}/knowledge", json={
        "client_id": "reference-blog-post-1", "kind": "journal", "title": "Linked work", "body": body,
        "state": "published", "scope_kind": "project", "scope_id": project["id"]})
    assert post.status_code == 201, post.text
    path = f"/blog/{org}/{post.json()['id']}"
    page = client.get(path).text
    assert f"href=\"/organization.html?organization={org}&amp;topic={topics[0]['id']}\"" in page
    assert ">α-alpha</a>" in page and ">Hidden program</a>" in page and ">α-Hidden lemma</a>" in page
    assert f"<code>#[topic:{hidden_topic['id']}]</code>" in page
    _, member, _ = invited(installation)
    page = member.get(path).text
    assert ">α-alpha</a>" in page and "Hidden" not in page
    assert page.count('<span class="blog-reference blog-reference-unavailable">unavailable</span>') == 2
    assert member.get(path + ".md").text == body
