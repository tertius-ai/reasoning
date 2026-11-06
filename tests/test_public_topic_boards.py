"""Topic boards: cards, routes and boards inside cards, arrangements the model applies once, and side questions about a card."""

from __future__ import annotations

import json

import pytest
from test_public_inference import MODELS, QUOTES, prepare
from test_public_server import installation as installation
from test_public_server import invited

from reasoning_ledger.server.openrouter import Quote, Usage
from reasoning_ledger.server.research_tools import ResearchTools
from reasoning_ledger.server.side_chat import SideChats
from reasoning_ledger.server.topic_boards import BOARD_TOOLS, TopicBoards

QUOTE = Quote(**{**QUOTES[0].record(), "supports_tools": True})
QUESTION = "How do we show the motivic regulator is injective for number fields?"


@pytest.fixture
def board(installation):
    _, state, client, owner, topics = prepare(installation)
    org, topic = owner["organization_id"], topics[1]["id"]
    root = f"/api/organizations/{org}"
    created = client.post(f"{root}/topics/{topic}/boards", json={"question": QUESTION})
    assert created.status_code == 201, created.text
    path = f"{root}/boards/{created.json()['id']}"
    cards = {}
    for name in ("Reduce to Borel's theorem", "Use Suslin rigidity", "Check Q(sqrt -3) numerically"):
        view = client.post(path + "/cards", json={"text": name}).json()
        cards[name] = next(card["id"] for card in view["cards"] if card["text"] == name)
    return {"state": state, "client": client, "owner": owner, "org": org, "topic": topic, "topics": topics, "root": root,
            "path": path, "id": created.json()["id"], "cards": cards, "installation": installation}


def arrange(board, client_id="arrange"):
    parameters = board["state"].parameters([MODELS[0]], 512, 50, None)
    runs = TopicBoards(board["state"]).enqueue(board["owner"]["user_id"], board["org"], board["id"], client_id, parameters, [QUOTE])
    return runs[0]


def test_cards_routes_and_boards_inside_cards(board):
    client, path, cards = board["client"], board["path"], board["cards"]
    borel, suslin, check = (cards[name] for name in cards)
    assert client.post(path + "/links", json={"source_card_id": borel, "target_card_id": suslin}).status_code == 201
    view = client.post(path + "/links", json={"source_card_id": suslin}).json()
    assert {(link["source_card_id"], link["target_card_id"], link["kind"]) for link in view["links"]} == {
        (borel, suslin, "route"), (suslin, None, "route")}
    # Linking twice keeps one link.
    assert len(client.post(path + "/links", json={"source_card_id": suslin}).json()["links"]) == 2
    child = client.post(f"{path}/cards/{suslin}/board", json={})
    assert child.status_code == 200, child.text
    child = child.json()
    assert child["question"] == "Use Suslin rigidity" and child["parent_card_id"] == suslin
    assert child["path"] == [{"board_id": board["id"], "question": QUESTION, "card_id": suslin, "card": "Use Suslin rigidity"}]
    assert client.post(f"{path}/cards/{suslin}/board", json={}).json()["id"] == child["id"], "a card opens one board"
    client.post(f"{board['root']}/boards/{child['id']}/cards", json={"text": "Rigidity over the algebraic closure"}).raise_for_status()
    parent = client.get(path).json()
    card = next(item for item in parent["cards"] if item["id"] == suslin)
    assert card["board"] == {"id": child["id"], "cards": 1, "gaps": 0}
    assert [item["text"] for item in parent["children"][suslin]["cards"]] == ["Rigidity over the algebraic closure"]
    # Renaming a card renames the question of the board inside it.
    client.patch(f"{path}/cards/{suslin}", json={"text": "Use Suslin rigidity for K3"}).raise_for_status()
    assert client.get(f"{board['root']}/boards/{child['id']}").json()["question"] == "Use Suslin rigidity for K3"
    # Removing a card removes its links.
    view = client.delete(f"{path}/cards/{borel}").json()
    assert borel not in [item["id"] for item in view["cards"]]
    assert [(link["source_card_id"], link["target_card_id"]) for link in view["links"]] == [(suslin, None)]
    assert client.post(path + "/links", json={"source_card_id": check, "target_card_id": check}).status_code == 400
    listed = client.get(f"{board['root']}/topics/{board['topic']}/boards").json()
    assert [item["id"] for item in listed] == [board["id"]], "boards inside cards are not listed as the topic's boards"


def test_boards_need_a_beta_topic_and_writers_change_them(board):
    root, alpha = board["root"], board["topics"][0]["id"]
    assert board["client"].post(f"{root}/topics/{alpha}/boards", json={"question": "Why?"}).status_code == 400
    _, viewer, _ = invited(board["installation"], role="viewer", email="viewer@example.org")
    assert viewer.get(board["path"]).json()["question"] == QUESTION
    assert viewer.post(board["path"] + "/cards", json={"text": "A viewer idea"}).status_code == 403
    assert viewer.post(board["path"] + "/arrange", json={"models": [MODELS[0]], "client_id": "viewer"}).status_code == 403
    assert board["client"].get(f"{root}/boards/{'f' * 32}").status_code == 404


def test_an_arrangement_is_applied_once_and_never_posted_as_a_reply(board):
    client, path, cards, state = board["client"], board["path"], board["cards"], board["state"]
    borel, suslin, check = (cards[name] for name in cards)
    client.post(path + "/links", json={"source_card_id": check, "target_card_id": borel}).raise_for_status()
    run = arrange(board)
    with state.store.transaction() as db:
        context = json.loads(db.execute("SELECT context_json FROM llm_requests WHERE id=?", (run["request_id"],)).fetchone()[0])
    assert QUESTION in json.dumps(context) and "board_arrange" in context[-1]["content"]
    assert client.get(path).json()["arranging"]["status"] == "queued"
    assert client.post(path + "/arrange", json={"models": [MODELS[0]], "client_id": "again"}).status_code == 409
    row = state.claim()
    tools = ResearchTools(state, row)
    names = {tool.name for tool in tools.registry.list()}
    assert "board_arrange" in names and names <= BOARD_TOOLS | {"tool_search", "tool_call"}
    assert not names & {"ledger_claim", "artifact_write", "agent_delegate", "compute_propose"}
    arrangement = {
        "cards": [{"card_id": borel, "rank": 1, "maturity": "settled", "theme": "Via Borel", "promise": 0.9, "effort": 0.2,
                   "reason": "Borel's theorem settles the rational case."},
                  {"card_id": suslin, "rank": 2, "maturity": "exploring", "theme": "Via Suslin", "promise": 0.7, "effort": 0.8}],
        "gaps": [{"key": "limit", "text": "Missing: why the limit commutes", "theme": "Via Suslin"}],
        "routes": [[borel], [suslin, "limit"]],
        "evidence": [check],
    }
    # A refused arrangement changes nothing, and a run applies only one.
    with pytest.raises(ValueError, match="not on this board"):
        tools.invoke("board_arrange", {**arrangement, "routes": [["f" * 32]]})
    assert all(card["rank"] is None for card in client.get(path).json()["cards"])
    assert tools.invoke("board_arrange", arrangement)["result"]["applied"] is True
    with pytest.raises(ValueError, match="already arranged"):
        tools.invoke("board_arrange", arrangement)
    assert state.progress(row, "Arranged the board.", None)
    assert state.finish(row, "succeeded", usage=Usage(10, 20, 5))
    view = client.get(path).json()
    by_id = {card["id"]: card for card in view["cards"]}
    assert (by_id[borel]["rank"], by_id[borel]["maturity"], by_id[borel]["theme"]) == (1, "settled", "Via Borel")
    assert by_id[suslin]["promise"] == 0.7 and by_id[check]["rank"] is None
    gap = next(card for card in view["cards"] if card["kind"] == "gap")
    assert (gap["text"], gap["author_kind"]) == ("Missing: why the limit commutes", "assistant")
    assert {(link["source_card_id"], link["target_card_id"], link["kind"], link["author_kind"]) for link in view["links"]} == {
        (check, borel, "route", "human"), (borel, None, "route", "assistant"), (suslin, gap["id"], "route", "assistant"),
        (gap["id"], None, "route", "assistant"), (check, None, "evidence", "assistant")}
    assert view["arranged_at"] and view["arranging"]["status"] == "succeeded" and view["arranging"]["applied_at"]
    assert run["id"] not in [item["id"] for item in client.get(f"{board['root']}/topics/{board['topic']}/inference-runs").json()]
    assert all(item["content"] != "Arranged the board." for item in client.get(f"{board['root']}/topics/{board['topic']}/messages").json())
    # A later arrangement replaces the model's gaps and links; people's links stay.
    second = arrange(board, "arrange-again")
    row = state.claim()
    assert row["id"] == second["id"]
    ResearchTools(state, row).invoke("board_arrange", {"cards": [], "routes": [[suslin]]})
    view = client.get(path).json()
    assert [card for card in view["cards"] if card["kind"] == "gap"] == []
    assert {(link["source_card_id"], link["target_card_id"], link["author_kind"]) for link in view["links"]} == {
        (check, borel, "human"), (suslin, None, "assistant")}


def test_a_side_question_about_a_card_carries_the_card(board):
    suslin, borel = board["cards"]["Use Suslin rigidity"], board["cards"]["Reduce to Borel's theorem"]
    board["client"].post(board["path"] + "/links", json={"source_card_id": borel, "target_card_id": suslin}).raise_for_status()
    side_path = f"{board['root']}/topics/{board['topic']}/side-chat"
    asked = board["client"].post(side_path + "/messages", json={"content": "Does rigidity give it?", "client_id": "about",
                                                                "card_id": suslin})
    assert asked.status_code == 201, asked.text
    assert asked.json()["card"] == {"id": suslin, "board_id": board["id"], "text": "Use Suslin rigidity"}
    assert board["client"].post(side_path + "/messages", json={"content": "Elsewhere", "client_id": "missing",
                                                               "card_id": "f" * 32}).status_code == 404
    parameters = board["state"].parameters([MODELS[0]], 256, 50, None)
    run = SideChats(board["state"]).enqueue(board["owner"]["user_id"], board["org"], board["topic"], "about-reply", parameters,
                                            [QUOTE], asked.json()["id"])[0]
    with board["state"].store.transaction() as db:
        context = json.loads(db.execute("SELECT context_json FROM llm_requests WHERE id=?", (run["request_id"],)).fetchone()[0])
    about = next(message["content"] for message in context if "about this card" in message["content"])
    assert '"card":"Use Suslin rigidity"' in about and '"comes_from":["Reduce to Borel' in about and QUESTION in about
