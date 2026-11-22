"""A reply's steps are described in short, bounded phrases from its own tool events."""

import json
import sqlite3

from reasoning_ledger.server.reply_steps import completed, requested, run_activity

FETCH = {"name": "web_fetch", "arguments": {"url": "https://ncatlab.org/nlab/show/regulator?x=1"}}


def test_running_calls_read_as_what_they_are_doing():
    assert requested({"name": "arxiv_search", "arguments": {"query": "higher   regulators"}}) == "searching arXiv for “higher regulators”"
    assert requested({"name": "tool_call", "arguments": {"name": "crossref_search", "arguments": {"query": "Beilinson"}}}) \
        == "searching Crossref for “Beilinson”", "a dispatched call is described by the tool it ran"
    assert requested(FETCH) == "fetching ncatlab.org"
    assert requested({"name": "web_fetch", "arguments": {"url": "https://[broken"}}) == "fetching a page"
    assert requested({"name": "arxiv_read", "arguments": {"id": "1006.1116"}}) == "reading arXiv 1006.1116"
    saving = requested({"name": "artifact_write", "arguments": {"path": "notes/roadmap.tex", "text": "file body"}})
    assert saving == "saving notes/roadmap.tex"
    assert requested({"name": "topic_history", "arguments": {}}) == "reading earlier messages"
    assert requested({"name": "ledger_claim", "arguments": {}}) == "updating the ledger"
    assert requested({"name": "compute_submit", "arguments": {}}) == "submitting a computation"
    assert requested({"name": "mystery_tool"}) == "using mystery tool"
    long = requested({"name": "web_search", "arguments": {"query": "x" * 200}})
    assert long.startswith("searching the web for “") and len(long) < 90


def test_finished_calls_add_a_count_or_the_title_read_and_never_raw_output():
    search = {"name": "arxiv_search", "arguments": {"query": "regulators"}}
    found = {"ok": True, "result": {"receipt_id": "r", "result": {"results": [{"title": "A"}, {"title": "B"}], "text": "RAW"}}}
    assert completed(found, search) == "searched arXiv for “regulators” · 2 results"
    assert completed({"ok": True, "result": [{"id": 1}]}, search) == "searched arXiv for “regulators” · 1 result"
    read = {"ok": True, "result": {"title": "Higher regulators and values of L-functions", "body": "RAW TEXT"}}
    assert completed(read, {"name": "arxiv_read", "arguments": {"id": "1006.1116"}}) \
        == "read “Higher regulators and values of L-functions”"
    assert completed({"ok": True, "result": {"text": "RAW"}}, {"name": "ledger_claim", "arguments": {}}) == "updated the ledger"
    assert "RAW" not in completed(found, search) + completed(read, {"name": "arxiv_read"})


def test_a_fetch_reads_as_fetched_once_done_and_as_failed_only_when_it_failed():
    done = {"name": "web_fetch", "ok": True, "result": {"receipt_id": "r", "result": {"url": "u", "text": "RAW PAGE"}}}
    assert completed(done, FETCH) == "fetched ncatlab.org"
    failed = {"name": "web_fetch", "ok": False, "error": "The public source returned HTTP 403. RAW"}
    assert completed(failed, FETCH) == "couldn't fetch ncatlab.org"
    skipped = {"name": "web_fetch", "ok": False, "skipped": True, "error": "tool-call budget exhausted; finish without more tools"}
    assert completed(skipped, FETCH) == "skipped fetching ncatlab.org · no tool calls left", "a call that never ran did not fail"
    assert completed(done, None) == "fetched a page", "a request outside the recorded window still reads by its tool"
    search = {"name": "arxiv_search", "arguments": {"query": "regulators"}}
    assert completed({"ok": False, "error": "RAW ERROR"}, search) == "couldn't search arXiv for “regulators”"


def test_activity_describes_each_call_by_its_state_from_its_own_request():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE llm_events(organization_id TEXT, run_id TEXT, sequence INTEGER, event_json TEXT)")
    events = [
        {"phase": "round", "round": 1},
        {"phase": "tool_requested", "tool_call_id": "a", **FETCH},
        {"phase": "tool_completed", "tool_call_id": "a", "name": "web_fetch", "ok": False, "error": "redirected"},
        {"phase": "tool_requested", "tool_call_id": "b", "name": "web_fetch", "arguments": {"url": "https://example.org/x"}},
        {"phase": "tool_completed", "tool_call_id": "b", "name": "web_fetch", "ok": True, "result": {"text": "page"}},
        {"phase": "tool_requested", "tool_call_id": "c", "name": "web_fetch", "arguments": {"url": "https://mathoverflow.net/q/1"}},
    ]
    for sequence, body in enumerate(events, 1):
        db.execute("INSERT INTO llm_events VALUES('org','run',?,?)", (sequence, json.dumps(body)))
    details = [entry.get("detail") for entry in run_activity(db, "org", "run")]
    assert details == [None, "fetching ncatlab.org", "couldn't fetch ncatlab.org", "fetching example.org", "fetched example.org",
                       "fetching mathoverflow.net"]
