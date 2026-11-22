"""Provider requests are kept once per distinct message and read back exactly."""

import json
import os
import sqlite3
from contextlib import closing

from test_public_research_agent import ResearchFixture, turn
from test_public_server import installation as installation

from reasoning_ledger.server import maintenance
from reasoning_ledger.server.event_blobs import ENCODING, compact_requests, expand
from reasoning_ledger.server.event_blobs_schema import COMPACT_REQUESTS
from reasoning_ledger.server.postgres import migration


def stored_events(state, run):
    with state.store.transaction() as db:
        return [(row[0], json.loads(row[1])) for row in db.execute(
            "SELECT sequence,event_json FROM llm_events WHERE run_id=? ORDER BY sequence", (run["id"],))]


def test_requests_are_stored_once_and_every_reader_gets_the_exact_request(installation):
    fixture = ResearchFixture("long")
    state, owner, topics, run = turn(installation, fixture, maximum=None)
    org, user = owner["organization_id"], owner["user_id"]
    stored = [event for _, event in stored_events(state, run) if event.get("phase") == "provider_request"]
    assert len(stored) == len(fixture.requests) > 1
    assert all(event["request_encoding"] == ENCODING for event in stored)
    assert all(set(message) == {"$blob"} for event in stored for message in event["request"]["messages"])
    api = [item["event"] for item in state.events(user, org, run["id"], 0, 500)]
    assert [event["request"] for event in api if event.get("phase") == "provider_request"] == fixture.requests
    assert not any("request_encoding" in event for event in api)
    client = installation[2]
    export = client.get(f"/api/organizations/{org}/topics/{topics[1]['id']}/research-export").json()
    exported = [item["event"]["request"] for item in export["runs"][0]["events"] if item["event"].get("phase") == "provider_request"]
    assert exported == fixture.requests


def test_requests_recorded_inline_stay_readable_and_the_migration_compacts_them(installation):
    fixture = ResearchFixture("long")
    state, owner, _, run = turn(installation, fixture, maximum=None)
    org, user = owner["organization_id"], owner["user_id"]
    before = state.events(user, org, run["id"], 0, 500)
    with state.store.transaction(write=True) as db:
        # Rewrite the events as an earlier release recorded them: the full request inline.
        for sequence, event in stored_events(state, run):
            if event.get("phase") == "provider_request":
                db.execute("UPDATE llm_events SET event_json=? WHERE sequence=?",
                           (json.dumps(expand(db, org, event), ensure_ascii=False), sequence))
        db.execute("DELETE FROM llm_event_blobs")
    assert not any("request_encoding" in event for _, event in stored_events(state, run))
    assert state.events(user, org, run["id"], 0, 500) == before
    with state.store.transaction(write=True) as db:
        assert compact_requests(db) == len(fixture.requests)
        assert compact_requests(db) == 0
    assert all(event["request_encoding"] == ENCODING for _, event in stored_events(state, run)
               if event.get("phase") == "provider_request")
    assert state.events(user, org, run["id"], 0, 500) == before


def test_the_rewrite_is_a_no_op_statement_for_postgresql():
    assert migration(COMPACT_REQUESTS) == [COMPACT_REQUESTS]
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE llm_events(sequence INTEGER PRIMARY KEY, event_json TEXT)")
        db.execute("INSERT INTO llm_events VALUES(1,'{}')")
        db.execute(COMPACT_REQUESTS)
        assert db.execute("SELECT event_json FROM llm_events").fetchall() == [("{}",)]


def database_with_free_pages(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE kept(value TEXT)")
        db.execute("CREATE TABLE dropped(id INTEGER PRIMARY KEY, value TEXT)")
        db.executemany("INSERT INTO kept VALUES(?)", [(f"row {index}",) for index in range(300)])
        db.executemany("INSERT INTO dropped(value) VALUES(?)", [("x" * 4000,) for _ in range(500)])
        db.execute("DELETE FROM kept WHERE rowid % 3 = 0")
        db.execute("DELETE FROM dropped")
        db.commit()
        return db.execute("SELECT rowid,value FROM kept ORDER BY rowid").fetchall()


def test_compaction_removes_free_pages_and_keeps_every_row_and_row_id(tmp_path):
    path = tmp_path / "server.sqlite3"
    rows = database_with_free_pages(path)
    os.chmod(path, 0o600)
    size = path.stat().st_size
    maintenance._compact(path)
    assert path.stat().st_size < size / 4 and oct(path.stat().st_mode & 0o777) == "0o600"
    assert not (tmp_path / "server.sqlite3.compact").exists()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT rowid,value FROM kept ORDER BY rowid").fetchall() == rows
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    compacted = path.stat().st_size
    maintenance._compact(path)
    assert path.stat().st_size == compacted, "a file with few free pages is left as it is"


def test_compaction_leaves_a_database_with_a_write_ahead_log_alone(tmp_path):
    path = tmp_path / "server.sqlite3"
    database_with_free_pages(path)
    size = path.stat().st_size
    (tmp_path / "server.sqlite3-wal").write_bytes(b"")
    maintenance._compact(path)
    assert path.stat().st_size == size and not (tmp_path / "server.sqlite3.compact").exists()
