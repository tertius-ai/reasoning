"""Research tool receipt lists: compact status summaries and complete, access-controlled details."""

import json
import time

from test_public_research_safety import research as research
from test_public_research_safety import start
from test_public_server import installation as installation
from test_public_server import invited

# Receipt payloads sized like the audited topic: 50 rows decoded to about 520 kB.
ARGUMENTS = {"query": "q" * 2000, "filters": ["x" * 200] * 5}
RESULT = {"items": [{"title": "t" * 300, "body": "b" * 700}] * 2}


def receipts(research, count=50):
    store, client, owner, topic, _, _ = research
    _, row, _ = start(research)
    org, now = owner["organization_id"], int(time.time())
    with store.transaction(write=True) as db:
        db.execute("INSERT INTO research_sessions VALUES(?,?,?,?,?)", (row["id"], org, topic, "{}", now))
        for sequence in range(1, count + 1):
            pending = sequence == count
            db.execute(
                "INSERT INTO research_tool_receipts(id,organization_id,run_id,sequence,name,arguments_json,result_json,status,"
                "created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"receipt-{sequence:03}", org, row["id"], sequence, "web_search", json.dumps(ARGUMENTS),
                 None if pending else json.dumps(RESULT), "requested" if pending else "completed",
                 now - count + sequence, None if pending else now),
            )
    return client, f"/api/organizations/{org}/topics/{topic}/research-tools", now


def test_summary_list_is_bounded_and_omits_arguments_and_results(research):
    client, path, now = receipts(research)
    full = client.get(path, params={"after": 0})
    summary = client.get(path, params={"after": 0, "summary": "1"})
    assert full.status_code == summary.status_code == 200
    rows = summary.json()
    assert [row["id"] for row in rows] == [row["id"] for row in full.json()]
    assert rows[0] == {"id": "receipt-050", "name": "web_search", "status": "requested", "created_at": now, "finished_at": None}
    # Fifty audited-size receipts: about 520 kB complete, under 6 kB as summaries.
    assert len(full.content) > 500_000 and len(summary.content) < 6_000, (len(full.content), len(summary.content))
    assert "qqqq" not in summary.text and "arguments" not in summary.text and "result" not in summary.text
    # The cursor is inclusive in both forms.
    assert [row["id"] for row in client.get(path, params={"after": now, "summary": "true"}).json()] == ["receipt-050"]


def test_full_list_and_detail_keep_exact_arguments_and_results(research):
    client, path, _ = receipts(research, count=2)
    listed = client.get(path).json()
    assert listed[1]["arguments"] == ARGUMENTS and listed[1]["result"] == RESULT
    assert json.loads(listed[1]["arguments_json"]) == ARGUMENTS
    detail = client.get(path + "/receipt-001").json()
    assert detail["arguments"] == ARGUMENTS and detail["result"] == RESULT and detail["status"] == "completed"


def test_pending_receipt_status_change_is_visible_in_summaries(research):
    store, *_ = research
    client, path, now = receipts(research, count=3)
    assert client.get(path, params={"after": now, "summary": 1}).json()[0]["status"] == "requested"
    with store.transaction(write=True) as db:
        db.execute("UPDATE research_tool_receipts SET status='completed',result_json='{}',finished_at=? WHERE id='receipt-003'", (now + 1,))
    assert client.get(path, params={"after": now, "summary": 1}).json() == [
        {"id": "receipt-003", "name": "web_search", "status": "completed", "created_at": now, "finished_at": now + 1}]


def test_summaries_and_details_require_topic_access(research, installation):
    _, _, owner, _, _, _ = research
    client, path, _ = receipts(research, count=2)
    _, member, user = invited(installation)
    assert member.get(path, params={"summary": 1}).status_code == 200
    assert client.delete(f"/api/organizations/{owner['organization_id']}/members/" + user["user_id"]).status_code == 204
    for denied in (member.get(path, params={"summary": 1}), member.get(path), member.get(path + "/receipt-001")):
        assert denied.status_code in {403, 404}
        assert "qqqq" not in denied.text
