"""Authorized bot topic discovery and source labels; no network or paid inference.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import pytest
from test_public_bots import fixture as bot_fixture
from test_public_bots import member

from reasoning_ledger.server.bot_pairing import BotPairing
from reasoning_ledger.server.bot_runtime import BotRuntime
from reasoning_ledger.server.bot_sharing import BotSharing
from reasoning_ledger.server.bot_tools import BotTools
from reasoning_ledger.server.security import digest
from reasoning_ledger.server.store import StoreError


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


def invocation(fixture, *, owner_id=None, requester=None, following=True):
    store, owner, topics, _, _, bots = fixture
    org = owner["organization_id"]
    user = owner_id or owner["user_id"]
    bot = bots.create(user, org, "Discoverer", [] if following else topics[1:],
                      ["messages:read", "messages:write"], personal=True, public_access=following)
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], False, False)
    BotSharing(bots).configure(user, org, bot["id"], "topic_writers")
    run = runtime.enqueue(requester or user, org, topics[1], f"bot/{bot['id']}/default", "discover", prompt="Find a topic")
    claimed = runtime.claim(bot["token"])
    tools = BotTools(runtime)
    return tools, (bot["token"], run["id"], claimed["lease"])


def invoke(tools, lease, name, **arguments):
    return tools.invoke(*lease, name, arguments, "discovery-test")


def location(fixture, project_name, channel_name, title, *, private=False):
    store, owner, _, _, _, _ = fixture
    user, org = owner["user_id"], owner["organization_id"]
    project = store.create_entity(user, org, "projects", project_name,
                                  visibility="private" if private else "organization", owner_id=user if private else None)
    channel = store.create_entity(user, org, "channels", channel_name, project["id"])
    topic = store.create_entity(user, org, "topics", title, channel["id"], "gamma")
    return project, channel, topic


def test_context_and_search_identify_accessible_topic_by_full_location(fixture):
    store, owner, topics, _, _, _ = fixture
    tools, lease = invocation(fixture)
    readiness = invoke(tools, lease, "topic_context")
    assert readiness["ready"] and "topics_search" in readiness["tools"]
    scope = readiness["scope"]
    assert scope["topic_id"] == topics[1]
    assert scope["topic_title"] == "beta" and scope["topic_kind"] == "beta"
    assert scope["channel_name"] == "Channel" and scope["project_name"] == "Project"
    assert scope["organization_id"] == owner["organization_id"]
    store.post_message(owner["user_id"], owner["organization_id"], topics[1], "Needle answer", "answer")
    result = invoke(tools, lease, "messages_search", query="Needle")
    assert len(result) == 1 and result[0]["content"] == "Needle answer"
    assert {key: result[0][key] for key in scope} == scope


def test_discovery_finds_empty_new_topics_and_keeps_ambiguous_titles_separate(fixture):
    tools, lease = invocation(fixture)
    p, c, beta = location(fixture, "Demo", "Tate", "beta-surfaces")
    store, owner, _, _, _, _ = fixture
    gamma = store.create_entity(owner["user_id"], owner["organization_id"], "topics", "gamma-surfaces", c["id"], "gamma")
    other_project, other_channel, duplicate = location(fixture, "Other demo", "Tate", "beta-surfaces")
    matches = invoke(tools, lease, "topics_search", query="SURFACES", channel_name="tAtE")
    assert {item["topic_id"] for item in matches} == {beta["id"], gamma["id"], duplicate["id"]}
    assert all(item["channel_name"] == "Tate" for item in matches)
    assert invoke(tools, lease, "messages_search", query="surfaces") == []
    narrowed = invoke(tools, lease, "topics_search", query="beta-surfaces", project_name="dEmO", channel_name="Tate")
    assert [item["topic_id"] for item in narrowed] == [beta["id"]]
    assert narrowed[0]["project_id"] == p["id"] and narrowed[0]["channel_id"] == c["id"]
    assert [item["topic_id"] for item in invoke(tools, lease, "topics_search", project_id=other_project["id"],
        channel_id=other_channel["id"], topic_id=duplicate["id"])] == [duplicate["id"]]
    assert invoke(tools, lease, "topics_search", project_id=p["id"], topic_id=duplicate["id"]) == []
    assert len(invoke(tools, lease, "topics_search", query="surfaces", limit=2)) == 2


def test_labels_follow_renames_without_following_other_organizations(fixture):
    store, owner, topics, _, foreign, _ = fixture
    tools, lease = invocation(fixture)
    scope = invoke(tools, lease, "topic_context")["scope"]
    for kind, identity, name in (("projects", scope["project_id"], "Renamed project"),
                                 ("channels", scope["channel_id"], "Renamed channel"),
                                 ("topics", topics[1], "Renamed topic")):
        store.rename_entity(owner["user_id"], owner["organization_id"], kind, identity, name)
    current = invoke(tools, lease, "topic_context")["scope"]
    assert current["topic_title"] == "Renamed topic"
    assert current["channel_name"] == "Renamed channel" and current["project_name"] == "Renamed project"
    assert invoke(tools, lease, "topics_search", topic_id=topics[1]) == [current]
    assert invoke(tools, lease, "topics_search", topic_id=foreign) == []
    assert invoke(tools, lease, "topics_search", query="Other") == []
    assert invoke(tools, lease, "messages_search", query="Needle", topic_id=foreign) == []


def test_explicit_bot_grants_do_not_disclose_other_authorized_user_topics(fixture):
    store, owner, topics, _, _, _ = fixture
    tools, lease = invocation(fixture, following=False)
    store.post_message(owner["user_id"], owner["organization_id"], topics[0], "Needle ungranted", "ungranted")
    assert {item["topic_id"] for item in invoke(tools, lease, "topics_search")} == set(topics[1:])
    assert invoke(tools, lease, "topics_search", topic_id=topics[0]) == []
    assert invoke(tools, lease, "topics_search", query="alpha") == []
    assert invoke(tools, lease, "messages_search", query="Needle") == []


@pytest.mark.parametrize("member_owns_bot", [False, True])
def test_private_metadata_and_messages_require_both_requester_and_owner_access(fixture, member_owns_bot):
    store, owner, _, _, _, _ = fixture
    user, org = owner["user_id"], owner["organization_id"]
    other = member(fixture)
    project, _, secret = location(fixture, "Secret project", "Secret channel", "Secret title", private=True)
    bot_owner, requester = (other, user) if member_owns_bot else (user, other)
    tools, lease = invocation(fixture, owner_id=bot_owner, requester=requester)
    store.post_message(user, org, secret["id"], "Needle private", "secret")
    assert invoke(tools, lease, "topics_search", query="Secret") == []
    assert invoke(tools, lease, "topics_search", topic_id=secret["id"]) == []
    assert invoke(tools, lease, "messages_search", query="Needle") == []
    store.change_project_member(user, org, project["id"], other, "member")
    assert [item["topic_id"] for item in invoke(tools, lease, "topics_search", query="Secret")] == [secret["id"]]
    assert invoke(tools, lease, "messages_search", query="Needle")[0]["topic_title"] == "Secret title"
    store.change_project_member(user, org, project["id"], other, None)
    assert invoke(tools, lease, "topics_search", project_name="Secret project") == []
    assert invoke(tools, lease, "messages_search", query="Needle", topic_id=secret["id"]) == []


def test_deleted_topics_and_literal_wildcards(fixture):
    store, owner, _, _, _, _ = fixture
    tools, lease = invocation(fixture)
    _, _, topic = location(fixture, "Literal", "No wildcards", "100%_\\done")
    assert [item["topic_id"] for item in invoke(tools, lease, "topics_search", query="%_\\")] == [topic["id"]]
    store.post_message(owner["user_id"], owner["organization_id"], topic["id"], "100%_\\done", "literal")
    assert len(invoke(tools, lease, "messages_search", query="%_\\")) == 1
    plan = store.deletion_plan(owner["user_id"], owner["organization_id"], "topics", topic["id"])
    store.delete_entity(owner["user_id"], owner["organization_id"], "topics", topic["id"], topic["name"], plan["plan_token"])
    assert invoke(tools, lease, "topics_search", topic_id=topic["id"]) == []
    assert invoke(tools, lease, "messages_search", query="%_\\") == []


@pytest.mark.parametrize("name, arguments", [
    ("topics_search", {"limit": 21}), ("topics_search", {"topic_id": "not-an-id"}),
    ("topics_search", {"organization_id": "a" * 32}), ("topics_search", {"query": "x" * 201}),
    ("messages_search", {"query": ""}),
])
def test_discovery_is_bounded_and_has_no_caller_selected_organization(fixture, name, arguments):
    tools, lease = invocation(fixture)
    with pytest.raises(StoreError, match="invalid mathematical tool arguments"):
        invoke(tools, lease, name, **arguments)


def test_browser_pairing_binds_to_the_selected_organization(fixture):
    _, owner, _, other_org, other_topic, bots = fixture
    pairing = BotPairing(bots)
    credential, code, device = "rnb_" + "c" * 43, "ABCDEFGH2345", "d" * 43
    pairing.start(device, code, digest(credential), "Selected org bot", "codex")
    pairing.approve(owner["user_id"], other_org, code, [other_topic], ["messages:read", "messages:write"], 3600)
    assert bots.identity(credential)["organization_id"] == other_org
    assert bots.identity(credential)["topic_ids"] == [other_topic]
    with pytest.raises(StoreError, match="different access"):
        pairing.approve(owner["user_id"], owner["organization_id"], code, [], ["messages:read"], 3600, public_access=True)


def test_composed_mcp_gateway_discovers_read_tool_and_preserves_topic_labels(fixture, tmp_path):
    from reasoning_ledger.server.bot_gateway import Gateway

    tools, lease = invocation(fixture)

    class Client:
        def _request(self, method, path, body):
            assert method == "POST" and path.startswith(f"/api/bot/runs/{lease[1]}/")
            if path.endswith("/context"):
                return tools.context(*lease)
            if path.endswith("/tools"):
                return tools.invoke(*lease, body["name"], body["arguments"], body["client_id"])
            assert path.endswith("/gateway")
            return {"ready": True}

    registry = Gateway({"run_id": lease[1], "lease": lease[2], "workspace": str(tmp_path),
                        "local_tools": False}, client=Client()).registry()
    assert next(tool for tool in registry.list() if tool.name == "topics_search").risk == "read"
    assert "topics_search" in {tool["name"] for tool in registry.invoke("tool_search", {"query": "topics_search"})}
    result = registry.invoke("tool_call", {"name": "topics_search", "arguments": {"query": "beta"}})["result"]
    scope = registry.invoke("topic_context", {})["scope"]
    assert result == [scope] and scope["topic_title"] == "beta"
