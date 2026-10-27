"""A reply's steps are described in short, bounded phrases from its own tool events."""

from reasoning_ledger.server.reply_steps import completed, requested


def test_tool_calls_read_as_short_phrases_about_what_they_did():
    assert requested({"name": "arxiv_search", "arguments": {"query": "higher   regulators"}}) == "searched arXiv for “higher regulators”"
    assert requested({"name": "tool_call", "arguments": {"name": "crossref_search", "arguments": {"query": "Beilinson"}}}) \
        == "searched Crossref for “Beilinson”", "a dispatched call is described by the tool it ran"
    assert requested({"name": "web_fetch", "arguments": {"url": "https://ncatlab.org/nlab/show/regulator?x=1"}}) == "fetched ncatlab.org"
    assert requested({"name": "arxiv_read", "arguments": {"id": "1006.1116"}}) == "read arXiv 1006.1116"
    saved = requested({"name": "artifact_write", "arguments": {"path": "notes/roadmap.tex", "text": "file body"}})
    assert saved == "saved notes/roadmap.tex"
    assert requested({"name": "topic_history", "arguments": {}}) == "read earlier messages"
    assert requested({"name": "ledger_claim", "arguments": {}}) == "updated the ledger"
    assert requested({"name": "mystery_tool"}) == "mystery tool"
    long = requested({"name": "web_search", "arguments": {"query": "x" * 200}})
    assert long.startswith("searched the web for “") and len(long) < 90


def test_finished_calls_add_a_count_or_the_title_read_and_never_raw_output():
    asked = "searched arXiv for “regulators”"
    found = {"ok": True, "result": {"receipt_id": "r", "result": {"results": [{"title": "A"}, {"title": "B"}], "text": "RAW"}}}
    assert completed(found, asked) == "searched arXiv for “regulators” · 2 results"
    assert completed({"ok": True, "result": [{"id": 1}]}, asked) == "searched arXiv for “regulators” · 1 result"
    read = {"ok": True, "result": {"title": "Higher regulators and values of L-functions", "body": "RAW TEXT"}}
    assert completed(read, "read arXiv 1006.1116") == "read “Higher regulators and values of L-functions”"
    assert completed({"ok": False, "error": "RAW ERROR"}, asked) == asked + " · failed"
    assert "RAW" not in completed(found, asked) + completed(read, "read arXiv x")
