"""The side chat's layout follows the account; whether it is open is kept per topic.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from fastapi.testclient import TestClient
from test_public_server import PUBLIC_URL, invited, structure
from test_public_server import installation as installation

from reasoning_ledger.server.api import create_app

LAYOUT = "/api/auth/side-chat"


def test_layout_is_personal_and_the_same_on_every_device(installation):
    config, _, client, owner = installation
    _, member_client, _ = invited(installation)
    assert client.get(LAYOUT).json() == {"desktop": {"mode": "side", "width": None, "floating": None}, "phone": {"height": 0.6}}
    chosen = {"desktop": {"mode": "float", "width": 0.5, "floating": {"x": 0.4, "y": 0.05, "w": 0.48, "h": 0.82}}, "phone": {"height": 1}}
    saved = client.put(LAYOUT, json=chosen)
    assert saved.status_code == 200, saved.text
    assert saved.json() == chosen
    second_device = TestClient(create_app(config), base_url=PUBLIC_URL)
    second_device.headers["Authorization"] = "Bearer " + owner["access_token"]
    assert second_device.get(LAYOUT).json() == chosen
    # Another person keeps their own layout.
    assert member_client.get(LAYOUT).json()["desktop"]["mode"] == "side"
    full = {"desktop": {"mode": "full", "width": 0.5, "floating": None}, "phone": {"height": 0.36}}
    assert member_client.put(LAYOUT, json=full).status_code == 200
    assert client.get(LAYOUT).json() == chosen
    second_device.headers.clear()
    assert second_device.get(LAYOUT).status_code == 401


def test_layout_rejects_shapes_the_app_cannot_draw(installation):
    _, _, client, _ = installation
    good = {"desktop": {"mode": "side", "width": 0.5, "floating": None}, "phone": {"height": 0.6}}
    for bad in (
        {**good, "desktop": {**good["desktop"], "mode": "sideways"}},
        {**good, "desktop": {**good["desktop"], "width": 1.5}},
        {**good, "desktop": {**good["desktop"], "floating": {"x": 0.1, "y": 0.1, "w": 0, "h": 0.5}}},
        {**good, "phone": {"height": 0.05}},
        {**good, "extra": True},
        {"desktop": good["desktop"]},
    ):
        assert client.put(LAYOUT, json=bad).status_code == 422, bad
    assert client.get(LAYOUT).json()["desktop"]["width"] is None


def test_open_state_is_kept_per_topic_and_per_person(installation):
    _, _, client, owner = installation
    org = owner["organization_id"]
    _, _, topics = structure(client, org)
    _, member_client, _ = invited(installation)
    root = f"/api/organizations/{org}"

    def state(reader):
        return {item["topic_id"]: item["side_chat_open"] for item in reader.get(root + "/hierarchy").json()["topic_state"]}

    assert set(state(client).values()) == {False}
    beta, gamma = topics[1]["id"], topics[2]["id"]
    assert client.put(f"{root}/topics/{beta}/side-chat", json={"open": True}).status_code == 204
    assert state(client)[beta] is True and state(client)[gamma] is False
    assert state(member_client)[beta] is False
    assert client.put(f"{root}/topics/{beta}/side-chat", json={"open": False}).status_code == 204
    assert state(client)[beta] is False
    # Opening it leaves the topic's read position and subscription alone.
    client.put(f"{root}/topics/{beta}/subscription", json={"subscribed": False})
    client.put(f"{root}/topics/{beta}/side-chat", json={"open": True})
    row = next(item for item in client.get(root + "/hierarchy").json()["topic_state"] if item["topic_id"] == beta)
    assert row["subscribed"] is False and row["side_chat_open"] is True and row["last_read_sequence"] == 0
    assert client.put(f"{root}/topics/{beta}/side-chat", json={"open": "yes"}).status_code == 422
    assert client.put(f"{root}/topics/{'f' * 32}/side-chat", json={"open": True}).status_code == 404
