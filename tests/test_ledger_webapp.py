"""Tests for the web workbench: project/chat/ledger API plus the
app-server chat session driven against a scripted fake codex
(tests/fake_appserver.py) — turns, steering, interrupts, approvals,
and the broadcast event stream that lets any number of windows watch
(or none: turns complete and record server-side regardless)."""

import http.client
import json
import queue
import re
import shlex
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reasoning_ledger import LedgerStore  # noqa: E402
from reasoning_ledger import webapp as webapp_module  # noqa: E402
from reasoning_ledger.store import LedgerStoreError  # noqa: E402
from reasoning_ledger.webapp import (  # noqa: E402
    ChatSession,
    _ledger_preflight,
    _map_item,
    _slim_entry,
    build_app,
    chat_last_assistant,
    read_tail_entries,
)

AUTHOR = {"operator": "op", "agent": "a", "session": "s"}
WORKBENCH_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
FAKE = [sys.executable, str(Path(__file__).parent / "fake_appserver.py")]


def _forget_derived_last_assistant():
    """Drop the per-chat answer cache, so a test can vary one chat's
    sidecars faster than the file stamps it is keyed on can resolve."""
    webapp_module._last_assistant_cache.clear()


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
    return TestClient(build_app())


def _session(tmp_path, **kwargs) -> ChatSession:
    (tmp_path / "scratch").mkdir(exist_ok=True)
    return ChatSession(
        tmp_path / "scratch",
        tmp_path / "ledger",
        "gpt-5.6-sol",
        "workspace-write",
        artifacts_dir=tmp_path / "artifacts",
        session_label="proj/chat-1",
        state_dir=tmp_path,
        codex_command=FAKE,
        **kwargs,
    )


def test_health_and_packaged_frontend(tmp_path, monkeypatch):
    dist = tmp_path / "web"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<h1>desktop frontend</h1>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
    monkeypatch.setenv("REASONING_WEB_DIST", str(dist))

    with TestClient(build_app()) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        index = client.get("/")
        assert index.text == "<h1>desktop frontend</h1>"
        assert index.headers["cache-control"] == "no-store"
        # The policy is a header: a <meta> policy cannot carry frame-ancestors.
        assert index.headers["content-security-policy"] == WORKBENCH_CSP
        favicon = client.get("/favicon.svg")
        assert favicon.text == "<svg></svg>"
        assert favicon.headers["content-type"] == "image/svg+xml"


def test_project_mutations_refresh_safe_explorer_bootstrap(tmp_path, monkeypatch):
    dist = tmp_path / "web"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><head></head><body></body></html>", encoding="utf-8")
    root = tmp_path / "workbench"
    monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(root))
    monkeypatch.setenv("REASONING_WEB_DIST", str(dist))

    with TestClient(build_app()) as client:
        created = client.post("/api/projects", json={"name": "Alpha </script>"})
        assert created.status_code == 200

        cache = json.loads((root / "settings" / "explorer-index.json").read_text(encoding="utf-8"))
        assert cache["projects"][0]["name"] == "Alpha </script>"

        html = client.get("/").text
        assert 'id="reasoning-explorer-bootstrap"' in html
        assert "Alpha \\u003c/script\\u003e" in html
        assert "Alpha </script>" not in html

        renamed = client.patch(
            f"/api/projects/{created.json()['id']}",
            json={"name": "Beta"},
        )
        assert renamed.status_code == 200
        cache = json.loads((root / "settings" / "explorer-index.json").read_text(encoding="utf-8"))
        assert cache["projects"][0]["name"] == "Beta"


def _collect(subscriber, until="turn.completed", timeout=15.0):
    """Drain a subscriber queue until an event of type `until` arrives."""
    events = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = subscriber.get(timeout=0.2)
        except queue.Empty:
            continue
        events.append(event)
        if event["type"] == until:
            return events
    raise AssertionError(f"no {until} within {timeout}s; saw {[e['type'] for e in events]}")


def _run(session, message, effort=None):
    """Start a turn with a live subscription; return its events."""
    _, subscriber = session.subscribe()
    assert session.start_turn(message, effort) == "started"
    events = _collect(subscriber)
    session.unsubscribe(subscriber)
    return events


def _wait_turn_idle(session, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline and session.turn_active:
        time.sleep(0.02)
    assert not session.turn_active


@contextmanager
def _live_server(app):
    """A real uvicorn server on a random port: the TestClient buffers
    whole responses, so the unbounded /events SSE needs actual HTTP."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "live workbench did not stop"


class TestHistoryTail:
    def test_command_activity_keeps_bounded_completion_details(self):
        output = "x" * 20_000
        mapped = _map_item(
            {
                "type": "commandExecution",
                "command": "pytest -q",
                "cwd": "/repo",
                "aggregatedOutput": output,
                "exitCode": 1,
                "status": "failed",
            }
        )

        assert mapped is not None
        assert mapped["cwd"] == "/repo"
        assert mapped["exit_code"] == 1
        assert mapped["output"].endswith("… output truncated")
        assert len(mapped["output"]) < len(output)

    def test_nonledger_activity_history_keeps_bounded_text_result(self):
        entry = {
            "role": "activity",
            "item": {
                "type": "mcp_tool_call",
                "server": "project",
                "tool": "artifact_read",
                "result": {"content": [{"type": "text", "text": "y" * 20_000}]},
            },
        }

        slim = _slim_entry(entry)
        text = slim["item"]["result"]["content"][0]["text"]
        assert text.endswith("… result truncated")
        assert len(text) < 20_000

    def test_ledger_activity_history_keeps_only_summary_fields(self):
        entry = {
            "role": "activity",
            "item": {
                "type": "mcp_tool_call",
                "server": "ledger",
                "tool": "ledger_status",
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"counts": {"established": 2}, "claims": ["large"]}),
                        }
                    ]
                },
            },
        }

        slim = _slim_entry(entry)
        text = slim["item"]["result"]["content"][0]["text"]
        assert json.loads(text) == {"counts": {"established": 2}}

    def test_read_tail_entries(self, tmp_path):
        path = tmp_path / "t.jsonl"
        with open(path, "w", encoding="utf-8") as handle:
            for i in range(10):
                handle.write(json.dumps({"n": i}) + "\n")

        entries, more = read_tail_entries(path, 3)
        assert [e["n"] for e in entries] == [7, 8, 9]
        assert more is True

        entries, more = read_tail_entries(path, 10)
        assert [e["n"] for e in entries] == list(range(10))
        assert more is False  # exactly the count, nothing above

        entries, more = read_tail_entries(path, 25)
        assert len(entries) == 10 and more is False

        # a line spanning several read chunks, and no trailing newline
        big = tmp_path / "big.jsonl"
        with open(big, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"n": 0, "b": "x" * 200000}) + "\n")
            handle.write(json.dumps({"n": 1}))  # no newline
        entries, more = read_tail_entries(big, 1)
        assert [e["n"] for e in entries] == [1] and more is True

    def test_last_assistant_comes_from_the_sidecars(self, tmp_path, monkeypatch):
        # what the project list needs to answer "unread?"; it must never
        # read the raw log, which runs to gigabytes
        chat = tmp_path / "chat-x"
        chat.mkdir()
        entries = [
            {"role": "user", "text": "q"},
            {"role": "assistant", "text": "a"},
            {"role": "activity", "item": {"type": "x"}},
        ]
        raw = chat / "chat-transcript.jsonl"
        slim = chat / "chat-transcript.slim.jsonl"
        for path in (raw, slim):
            with open(path, "w", encoding="utf-8") as handle:
                for entry in entries:
                    handle.write(json.dumps(entry) + "\n")
        record = {
            "count": 3,
            "raw_bytes": raw.stat().st_size,
            "slim_bytes": slim.stat().st_size,
        }
        meta = chat / "chat-transcript.meta.json"

        # the record answers outright when it carries the index
        meta.write_text(json.dumps({**record, "last_assistant": 1}), encoding="utf-8")
        assert chat_last_assistant(chat) == 1

        # a record written before it carried one: the slim log's tail
        # answers, and the raw log is never opened
        _forget_derived_last_assistant()
        meta.write_text(json.dumps(record), encoding="utf-8")
        raw.chmod(0o000)
        try:
            assert chat_last_assistant(chat) == 1
            # and the answer is cached against the files it came from, so
            # the tail is not re-read on the next listing
            slim.chmod(0o000)
            try:
                assert chat_last_assistant(chat) == 1
            finally:
                slim.chmod(0o644)
        finally:
            raw.chmod(0o644)

        # a field that is not a usable index is treated as absent: the
        # tail answers instead of the record shifting the answer
        for bad in (True, 3, -2, "1", None):
            _forget_derived_last_assistant()
            meta.write_text(json.dumps({**record, "last_assistant": bad}), encoding="utf-8")
            assert chat_last_assistant(chat) == 1

        # no assistant entry at all
        _forget_derived_last_assistant()
        meta.write_text(json.dumps({**record, "last_assistant": -1}), encoding="utf-8")
        assert chat_last_assistant(chat) == -1

        # reading past the entry the record's count places at the start of
        # the log means the two describe different logs: rejected, rather
        # than answered with an index shifted by the difference
        _forget_derived_last_assistant()
        meta.write_text(json.dumps({**record, "count": 1}), encoding="utf-8")
        assert chat_last_assistant(chat) == 1  # from the raw log instead

        # no usable record: a small raw log is parsed, a large one is not
        _forget_derived_last_assistant()
        meta.unlink()
        assert chat_last_assistant(chat) == 1
        _forget_derived_last_assistant()
        monkeypatch.setattr(webapp_module, "_LAST_ASSISTANT_RAW_BYTES", 8)
        assert raw.stat().st_size > 8
        assert chat_last_assistant(chat) == -1

    def test_last_assistant_survives_an_in_flight_append(self, tmp_path):
        # an append writes the raw log, then the slim log, then the
        # record: a listing landing in between must still answer, and the
        # recorded index is a valid lower bound because entries are only
        # ever appended
        chat = tmp_path / "chat-y"
        chat.mkdir()
        raw = chat / "chat-transcript.jsonl"
        slim = chat / "chat-transcript.slim.jsonl"
        lines = [
            json.dumps({"role": "user", "text": "q"}) + "\n",
            json.dumps({"role": "assistant", "text": "a"}) + "\n",
        ]
        for path in (raw, slim):
            path.write_text("".join(lines), encoding="utf-8")
        record = {
            "count": 2,
            "raw_bytes": raw.stat().st_size,
            "slim_bytes": slim.stat().st_size,
            "last_assistant": 1,
        }
        (chat / "chat-transcript.meta.json").write_text(json.dumps(record), encoding="utf-8")
        with open(raw, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "user", "text": "next"}) + "\n")
        raw.chmod(0o000)  # the raw log must not be read to answer
        try:
            assert chat_last_assistant(chat) == 1
        finally:
            raw.chmod(0o644)
        # a raw log that shrank is a different log: the record is dropped
        _forget_derived_last_assistant()
        raw.write_text(lines[0], encoding="utf-8")
        assert chat_last_assistant(chat) == -1

    def test_last_assistant_counts_entries_not_lines(self, tmp_path):
        # blank and crash-torn lines consume no index in the slim rebuild,
        # so they must consume none here either: the read marker this is
        # compared against lives in the entry index space
        chat = tmp_path / "chat-z"
        chat.mkdir()
        (chat / "chat-transcript.jsonl").write_text(
            json.dumps({"role": "user", "text": "q"}) + "\n"
            + "\n"
            + '{"role": "assistant", "te\n'
            + json.dumps({"role": "assistant", "text": "a"}) + "\n"
            + "\n"
            + json.dumps({"role": "activity", "item": {"type": "x"}}) + "\n",
            encoding="utf-8",
        )
        assert chat_last_assistant(chat) == 1

    def test_project_list_never_reads_a_raw_transcript(self, tmp_path, monkeypatch):
        # the list walks every chat in the workbench, so a chat with a
        # multi-gigabyte log must cost it nothing beyond the sidecars
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        directory = tmp_path / "workbench" / "projects" / project / "chats" / chat
        with open(directory / "chat-transcript.jsonl", "w", encoding="utf-8") as raw, open(
            directory / "chat-transcript.slim.jsonl", "w", encoding="utf-8"
        ) as slim:
            for index in range(6):
                line = json.dumps({"role": "assistant" if index % 2 else "user", "text": "t"})
                raw.write(line + "\n")
                slim.write(line + "\n")
        (directory / "chat-transcript.meta.json").write_text(
            json.dumps(
                {
                    "count": 6,
                    "raw_bytes": (directory / "chat-transcript.jsonl").stat().st_size,
                    "slim_bytes": (directory / "chat-transcript.slim.jsonl").stat().st_size,
                    "last_assistant": 5,
                }
            ),
            encoding="utf-8",
        )
        (directory / "chat-read.json").write_text(json.dumps({"index": 3}), encoding="utf-8")

        # unreadable raw log: the listing still answers, from the sidecars
        (directory / "chat-transcript.jsonl").chmod(0o000)
        try:
            listed = client.get("/api/projects").json()["projects"]
            assert [c["has_unread"] for p in listed for c in p["chats"]] == [True]
            (directory / "chat-read.json").write_text(json.dumps({"index": 5}), encoding="utf-8")
            listed = client.get("/api/projects").json()["projects"]
            assert [c["has_unread"] for p in listed for c in p["chats"]] == [False]
        finally:
            (directory / "chat-transcript.jsonl").chmod(0o644)

    def test_appending_keeps_the_assistant_index_current(self, tmp_path):
        session = _session(tmp_path)
        _run(session, "first question")
        record = json.loads(session.meta_path.read_text(encoding="utf-8"))
        assert record["last_assistant"] == record["count"] - 1
        assert chat_last_assistant(session.transcript_path.parent) == record["last_assistant"]
        # a rebuild from the raw log recovers the same index
        session.slim_path.unlink()
        session.meta_path.unlink()
        session._slim_count = session._transcript_count = None
        session._last_assistant = None
        session.snapshot()
        assert (
            json.loads(session.meta_path.read_text(encoding="utf-8"))["last_assistant"]
            == record["last_assistant"]
        )
        session.close()

    def test_history_serves_a_tail(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "big"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        transcript = (
            tmp_path / "workbench" / "projects" / project / "chats" / chat / "chat-transcript.jsonl"
        )
        with open(transcript, "w", encoding="utf-8") as handle:
            for i in range(500):
                handle.write(json.dumps({"role": "user", "text": f"m{i}"}) + "\n")

        base = f"/api/projects/{project}/chats/{chat}/history"
        tail = client.get(f"{base}?tail=200").json()
        assert len(tail["messages"]) == 200
        assert tail["messages"][0]["text"] == "m300"
        assert tail["messages"][-1]["text"] == "m499"
        assert tail["has_more"] is True

        full = client.get(f"{base}?tail=0").json()
        assert len(full["messages"]) == 500
        assert full["has_more"] is False

        deep = client.get(f"{base}?tail=600").json()
        assert len(deep["messages"]) == 500 and deep["has_more"] is False


class TestReadMarker:
    def test_marker_advances_monotonically(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        base = f"/api/projects/{project}/chats/{chat}"

        # untracked until the first post
        assert client.get(f"{base}/history").json()["read_index"] is None

        assert client.post(f"{base}/read", json={"index": -1}).json() == {"read_index": -1}
        assert client.post(f"{base}/read", json={"index": 7}).json() == {"read_index": 7}
        # never backward — a stale window cannot regress the marker
        assert client.post(f"{base}/read", json={"index": 3}).json() == {"read_index": 7}
        assert client.get(f"{base}/history").json()["read_index"] == 7

    def test_events_carry_transcript_indexes(self, tmp_path):
        session = _session(tmp_path)
        events = _run(session, "hello")
        recorded = [e["tidx"] for e in events if "tidx" in e]
        # the fake turn records entries 0..n in order, starting at the
        # user message
        assert recorded == list(range(len(recorded))) and recorded
        # a second turn continues the count where the transcript left off
        more = _run(session, "again")
        next_recorded = [e["tidx"] for e in more if "tidx" in e]
        assert next_recorded[0] == len(recorded)
        assert len(session.transcript()) == len(recorded) + len(next_recorded)


class TestReviewsPoll:
    def test_poll_is_slim_and_session_scoped(self, tmp_path, monkeypatch):
        from reasoning_ledger import service
        from reasoning_ledger.review import request_review, update_review

        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        store = LedgerStore(tmp_path / "workbench" / "projects" / project / "ledger")
        author = {"operator": "op", "agent": "a", "session": "p/chat-1"}
        ref = service.reference_payload(store, author="A", title="T")
        one = service.claim_payload(store, "One.", reference_id=ref["reference_id"], author=author)
        two = service.claim_payload(store, "Two.", reference_id=ref["reference_id"], author=author)
        done = request_review(store, claim_id=one["claim_id"], requested_by=author)
        update_review(
            store.root,
            done["review_id"],
            status="completed",
            verdict="verified",
            report="R" * 50_000,
            steps=["step " * 1000] * 5,
            usage={"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5},
        )
        running = request_review(store, claim_id=two["claim_id"], requested_by=author)

        base = f"/api/projects/{project}/reviews"
        scoped = client.get(f"{base}?for_session=p/chat-1").json()
        # only the active row rides the poll, and never the heavy fields
        assert [r["review_id"] for r in scoped["reviews"]] == [running["review_id"]]
        assert all("report" not in r and "steps" not in r for r in scoped["reviews"])
        # usage still covers the whole session history, summed server-side
        assert scoped["usage"] == {"input": 100, "cached": 40, "output": 5}
        assert len(client.get(f"{base}?for_session=p/chat-1").content) < 2000

        unfiltered = client.get(base).json()["reviews"]
        assert len(unfiltered) == 2
        assert all("report" not in r and "steps" not in r for r in unfiltered)

        detail = client.get(f"{base}/{done['review_id']}").json()
        assert detail["report"].startswith("R")
        assert len(detail["steps"]) == 5
        assert client.get(f"{base}/rev_missing").status_code == 404


class TestHistoryPhases:
    def test_turns_fetch_carries_absolute_indices_and_usage(self, tmp_path):
        session = _session(tmp_path)
        _run(session, "first question")
        _run(session, "second question")
        _run(session, "third question")
        session.usage_path.write_text(
            json.dumps(
                {
                    "total": {"inputTokens": 12, "outputTokens": 3},
                    "last": {"totalTokens": 15},
                    "modelContextWindow": 1000,
                }
            ),
            encoding="utf-8",
        )
        full, _ = session.snapshot()
        messages, _, start = session.turns_snapshot(2)
        assert messages == full[start:]
        # the slice begins exactly at the second-to-last user message
        user_indices = [i for i, m in enumerate(full) if m["role"] == "user"]
        assert start == user_indices[-2]
        # more turns than exist: everything, from index 0
        assert session.turns_snapshot(99)[2] == 0
        # a single "turn" with thousands of harness-driven entries must
        # not defeat the quick fetch: the entry cap bounds it
        session._TURNS_SNAPSHOT_CAP = 4
        capped, _, capped_start = session.turns_snapshot(2)
        assert len(capped) == 4
        assert capped == full[capped_start:]
        session.close()

    def test_slim_sidecar_is_kept_in_step(self, tmp_path):
        session = _session(tmp_path)
        _run(session, "first question")
        # first read builds the sidecar
        full, _ = session.snapshot()
        assert session.slim_path.exists()
        built = session.slim_path.stat().st_mtime_ns
        # a further turn appends to it rather than rebuilding
        _run(session, "second question")
        full2, _ = session.snapshot()
        assert len(full2) > len(full)
        with open(session.slim_path, encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
        assert len(lines) == len(full2)
        # deleting the sidecar rebuilds it transparently
        session.slim_path.unlink()
        session._slim_count = None
        rebuilt, _ = session.snapshot()
        assert rebuilt == full2
        assert session.slim_path.exists()
        del built
        session.close()

    def test_reopening_a_chat_never_scans_the_raw_log(self, tmp_path, monkeypatch):
        # a restart re-creates the session; with the sync record in place
        # the raw log is not read at all, and a crash between the two
        # appends is caught up by parsing only the new bytes
        session = _session(tmp_path)
        _run(session, "first question")
        full, _ = session.snapshot()
        session.close()

        reopened = _session(tmp_path)
        monkeypatch.setattr(
            reopened,
            "_rebuild_slim",
            lambda: (_ for _ in ()).throw(AssertionError("full rebuild ran")),
        )
        again, _ = reopened.snapshot()
        assert again == full
        # simulate the raw log gaining an entry the sidecar never saw
        # (plus a torn trailing line from a crash mid-write)
        with open(reopened.transcript_path, "ab") as handle:
            handle.write(json.dumps({"role": "user", "text": "orphaned"}).encode() + b"\n")
            handle.write(b'{"role": "assistant", "te')
        caught_up, _ = reopened.snapshot()
        assert [e["text"] for e in caught_up[-1:]] == ["orphaned"]
        assert len(caught_up) == len(full) + 1
        # the torn line is skipped, and the next append lands cleanly
        _run(reopened, "after the tear")
        final, _ = reopened.snapshot()
        assert final[len(full)]["text"] == "orphaned"
        assert final[-2]["text"] == "after the tear"
        reopened.close()

    def test_history_endpoint_turns_and_usage(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE)
        )
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        base = f"/api/projects/{project}/chats/{chat}"
        for text in ("one", "two", "three"):
            client.post(f"{base}/messages", json={"message": text})
            deadline = time.time() + 10
            while time.time() < deadline:
                if not client.get(f"{base}/history").json()["turn_active"]:
                    break
                time.sleep(0.1)
        full = client.get(f"{base}/history?tail=0").json()
        quick = client.get(f"{base}/history?turns=2").json()
        assert quick["has_more"] is True
        assert quick["start_index"] > 0
        assert (
            full["messages"][quick["start_index"] :] == quick["messages"]
        )
        assert full["start_index"] == 0
        # no turn ran through THIS webapp's usage events; the persisted
        # field is null rather than misleading zeros
        assert "usage" in full

        # scroll-back paging: the page ends exactly where the window
        # starts, and pages chain back to index 0
        page = client.get(
            f"{base}/history?before={quick['start_index']}&count=2"
        ).json()
        assert page["start_index"] == quick["start_index"] - 2
        assert (
            page["messages"]
            == full["messages"][page["start_index"] : quick["start_index"]]
        )
        first = client.get(f"{base}/history?before=2&count=100").json()
        assert first["start_index"] == 0
        assert first["has_more"] is False
        assert first["messages"] == full["messages"][:2]


class TestLedgerCaching:
    def test_status_etag_revalidates(self, tmp_path, monkeypatch):
        from reasoning_ledger import service

        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        store = LedgerStore(tmp_path / "workbench" / "projects" / project / "ledger")
        service.claim_payload(store, "First.", author=AUTHOR)
        base = f"/api/projects/{project}/ledger/status"

        first = client.get(base)
        etag = first.headers["etag"]
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-cache"
        # unchanged ledger: revalidation is a 304 with no body
        again = client.get(base, headers={"If-None-Match": etag})
        assert again.status_code == 304
        # a write invalidates
        service.claim_payload(store, "Second.", author=AUTHOR)
        changed = client.get(base, headers={"If-None-Match": etag})
        assert changed.status_code == 200
        assert changed.headers["etag"] != etag
        # claim detail and graph carry the same guard
        cid = changed.json()["claims"][0]["claim_id"]
        detail = client.get(f"/api/projects/{project}/ledger/claims/{cid}")
        assert detail.headers["etag"] == changed.headers["etag"]
        assert (
            client.get(
                f"/api/projects/{project}/ledger/claims/{cid}",
                headers={"If-None-Match": detail.headers["etag"]},
            ).status_code
            == 304
        )


class TestProjectLayoutAndRename:
    def test_slugs_fold_diacritics_and_hyphenate_removals(
        self, tmp_path, monkeypatch
    ):
        client = _client(tmp_path, monkeypatch)
        cases = {
            "étale reconstruction": "etale-reconstruction",
            "Kähler–Einstein: v2!": "kahler-einstein-v2",
            "Øresund ∞ theory": "resund-theory",
            "…": "project",
        }
        for name, expected in cases.items():
            created = client.post("/api/projects", json={"name": name}).json()
            assert created["id"] == expected, (name, created["id"])

    def test_new_projects_live_under_projects_dir(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        # a legacy project at the top level
        legacy = tmp_path / "workbench" / "legacy"
        (legacy / "chats").mkdir(parents=True)
        (legacy / "project.json").write_text('{"name": "Legacy"}\n')
        created = client.post("/api/projects", json={"name": "Fresh"}).json()
        assert (
            tmp_path / "workbench" / "projects" / created["id"] / "project.json"
        ).exists()
        listed = client.get("/api/projects").json()["projects"]
        assert {p["id"] for p in listed} == {"legacy", created["id"]}
        # both resolve through the same endpoints
        assert client.get("/api/projects/legacy/ledger/version").status_code == 200
        assert (
            client.get(f"/api/projects/{created['id']}/ledger/version").status_code
            == 200
        )
        # slug uniqueness across both layouts
        second = client.post("/api/projects", json={"name": "Legacy"}).json()
        assert second["id"] == "legacy-2"

    def test_move_and_archive(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        ids = [
            client.post("/api/projects", json={"name": n}).json()["id"]
            for n in ("alpha", "beta", "gamma")
        ]
        def order():
            return [
                p["id"]
                for p in client.get("/api/projects?include_archived=true").json()[
                    "projects"
                ]
            ]
        assert order() == ["alpha", "beta", "gamma"]
        client.post("/api/projects/gamma/move", json={"direction": "up"})
        assert order() == ["alpha", "gamma", "beta"]
        reordered = client.post(
            "/api/projects/reorder", json={"ids": ["beta", "alpha", "gamma"]}
        )
        assert reordered.status_code == 200
        assert reordered.json()["order"] == ["beta", "alpha", "gamma"]
        assert order() == ["beta", "alpha", "gamma"]
        # archiving flags without hiding server-side; moving skips the
        # archived neighbor
        client.patch("/api/projects/alpha", json={"archived": True})
        listed = client.get("/api/projects?include_archived=true").json()["projects"]
        assert [p["id"] for p in listed if p["archived"]] == ["alpha"]
        client.post("/api/projects/beta/move", json={"direction": "down"})
        # beta swaps with its visible neighbor gamma; archived alpha keeps its slot.
        assert order() == ["gamma", "alpha", "beta"]
        client.patch("/api/projects/alpha", json={"archived": False})
        assert all(
            p["archived"] is False
            for p in client.get("/api/projects?include_archived=true").json()["projects"]
        )
        # edges are no-ops, unknown directions rejected
        client.post("/api/projects/gamma/move", json={"direction": "up"})
        assert order() == ["gamma", "alpha", "beta"]
        assert (
            client.post(
                "/api/projects/alpha/move", json={"direction": "sideways"}
            ).status_code
            == 422
        )
        del ids

        # chats reorder and archive within their project
        chats = [
            client.post("/api/projects/alpha/chats", json={"title": t}).json()["id"]
            for t in ("one", "two")
        ]
        client.post(f"/api/projects/alpha/chats/{chats[1]}/move", json={"direction": "up"})
        listed = client.get("/api/projects?include_archived=true").json()["projects"]
        alpha = next(p for p in listed if p["id"] == "alpha")
        assert [c["id"] for c in alpha["chats"]] == [chats[1], chats[0]]
        reordered_chats = client.post(
            "/api/projects/alpha/chats/reorder",
            json={"ids": [chats[0], chats[1]]},
        )
        assert reordered_chats.status_code == 200
        listed = client.get("/api/projects?include_archived=true").json()["projects"]
        alpha = next(p for p in listed if p["id"] == "alpha")
        assert [c["id"] for c in alpha["chats"]] == chats
        assert client.post(
            "/api/projects/alpha/chats/reorder", json={"ids": [chats[0]]}
        ).status_code == 422
        client.patch(
            f"/api/projects/alpha/chats/{chats[0]}", json={"archived": True}
        )
        listed = client.get("/api/projects?include_archived=true").json()["projects"]
        alpha = next(p for p in listed if p["id"] == "alpha")
        assert [c["archived"] for c in alpha["chats"]] == [True, False]

    def test_new_chats_append_to_the_bottom(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        client.post("/api/projects", json={"name": "omega"})
        made = [
            client.post("/api/projects/omega/chats", json={"title": t}).json()["id"]
            for t in ("first", "second", "third")
        ]
        listed = client.get("/api/projects").json()["projects"]
        omega = next(p for p in listed if p["id"] == "omega")
        assert [c["id"] for c in omega["chats"]] == made
        # still the bottom after a reorder shuffles explicit positions
        client.post(
            f"/api/projects/omega/chats/{made[2]}/move", json={"direction": "up"}
        )
        fourth = client.post(
            "/api/projects/omega/chats", json={"title": "fourth"}
        ).json()["id"]
        listed = client.get("/api/projects").json()["projects"]
        omega = next(p for p in listed if p["id"] == "omega")
        assert [c["id"] for c in omega["chats"]] == [
            made[0],
            made[2],
            made[1],
            fourth,
        ]

    def test_spa_paths_serve_the_app(self, tmp_path, monkeypatch):
        dist = tmp_path / "web"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html><head></head><body></body></html>", encoding="utf-8")
        monkeypatch.setenv("REASONING_WEB_DIST", str(dist))
        client = _client(tmp_path, monkeypatch)
        page = client.get("/project/some-project/chat-abc")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert page.headers["content-security-policy"] == WORKBENCH_CSP
        # only the /project/ namespace belongs to the SPA
        assert client.get("/api/definitely-not-a-route").status_code == 404
        assert client.get("/some-top-level-thing").status_code == 404

    def test_rename_project_and_chat(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        renamed = client.patch(
            f"/api/projects/{project}", json={"name": "Better Name"}
        ).json()
        assert renamed["id"] == project
        assert renamed["name"] == "Better Name"
        assert renamed.get("archived", False) is False
        rechat = client.patch(
            f"/api/projects/{project}/chats/{chat}", json={"title": "the real work"}
        ).json()
        assert rechat["id"] == chat
        assert rechat["title"] == "the real work"
        assert rechat.get("archived", False) is False
        listed = client.get("/api/projects").json()["projects"]
        me = next(p for p in listed if p["id"] == project)
        assert me["name"] == "Better Name"
        assert me["chats"][0]["title"] == "the real work"
        assert (
            client.patch(f"/api/projects/{project}", json={"name": "  "}).status_code
            == 422
        )


class TestOverviewDelta:
    def test_status_since_serves_only_changes(self, tmp_path, monkeypatch):
        from reasoning_ledger import service

        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        store = LedgerStore(tmp_path / "workbench" / "projects" / project / "ledger")
        base = f"/api/projects/{project}/ledger/status"
        first = service.claim_payload(store, "First.", author=AUTHOR)

        full = client.get(base).json()
        v1 = full["version"]
        assert v1 and "delta_since" not in full
        assert [c["claim_id"] for c in full["claims"]] == [first["claim_id"]]

        # nothing changed: a tiny ack, no payload
        again = client.get(f"{base}?since={v1}").json()
        assert again == {"version": v1, "unchanged": True}

        # one write: the delta carries only the touched summaries
        second = service.claim_payload(store, "Second.", author=AUTHOR)
        delta = client.get(f"{base}?since={v1}").json()
        assert delta["delta_since"] == v1
        assert delta["version"] != v1
        assert [c["claim_id"] for c in delta["claims_changed"]] == [
            second["claim_id"]
        ]
        assert delta["claims_removed"] == []
        assert delta["references_changed"] == []
        assert "claims" not in delta
        # derived fields ride along in full
        assert delta["counts"] == {"conjectured": 2}

        # an unknown basis (server restarted, client far behind) → full
        stale = client.get(f"{base}?since=deadbeef").json()
        assert "claims" in stale and len(stale["claims"]) == 2

    def test_journal_serves_the_tail(self, tmp_path, monkeypatch):
        from reasoning_ledger import service

        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        store = LedgerStore(tmp_path / "workbench" / "projects" / project / "ledger")
        base = f"/api/projects/{project}/ledger/journal"
        service.claim_payload(store, "First.", author=AUTHOR)

        full = client.get(base).json()
        total = full["total"]
        assert total == len(full["entries"]) >= 1

        assert client.get(f"{base}?after={total}").json() == {
            "entries": [],
            "total": total,
        }
        service.claim_payload(store, "Second.", author=AUTHOR)
        tail = client.get(f"{base}?after={total}").json()
        assert tail["total"] == total + 1
        assert len(tail["entries"]) == 1
        assert tail["entries"][0]["event"] == "claim-created"


class TestArtifactUpload:
    def test_upload_dedupes_and_disambiguates(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        base = f"/api/projects/{project}/artifacts"

        first = client.post(f"{base}?name=notes.txt", content=b"alpha")
        assert first.status_code == 200
        assert first.json()["path"] == "artifacts/notes.txt"

        # same name, same content: reuse
        again = client.post(f"{base}?name=notes.txt", content=b"alpha")
        assert again.json()["path"] == "artifacts/notes.txt"

        # same name, different content: hash-suffixed, original untouched
        other = client.post(f"{base}?name=notes.txt", content=b"beta")
        stored = other.json()["path"]
        assert stored.startswith("artifacts/notes-") and stored.endswith(".txt")
        artifacts = tmp_path / "workbench" / "projects" / project / "artifacts"
        assert (artifacts / "notes.txt").read_bytes() == b"alpha"
        assert (artifacts / stored.removeprefix("artifacts/")).read_bytes() == b"beta"

        # the artifact endpoint serves what was stored
        served = client.get(f"/api/projects/{project}/{stored}")
        assert served.status_code == 200 and served.content == b"beta"

    def test_upload_rejects_bad_names(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        base = f"/api/projects/{project}/artifacts"
        assert client.post(f"{base}?name=.hidden", content=b"x").status_code == 422
        assert client.post(f"{base}?name=ok.txt", content=b"").status_code == 422
        # path components are stripped to the basename
        nested = client.post(f"{base}?name=..%2F..%2Fescape.txt", content=b"x")
        assert nested.json()["path"] == "artifacts/escape.txt"
        # whitespace would cut the cited path short in chat prose
        spaced = client.post(f"{base}?name=notes%20for%20model.txt", content=b"x")
        assert spaced.json()["path"] == "artifacts/notes-for-model.txt"

    def test_list_and_rename(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        base = f"/api/projects/{project}/artifacts"
        client.post(f"{base}?name=a.txt", content=b"alpha")
        client.post(f"{base}?name=b.txt", content=b"beta")
        (tmp_path / "workbench" / "projects" / project / "artifacts" / ".hidden").write_bytes(b"x")

        listed = client.get(base).json()["artifacts"]
        assert [a["path"] for a in listed] == ["a.txt", "b.txt"]  # dotfiles hidden
        assert listed[0]["size"] == 5
        assert listed[0]["modified_at"]

        # rename, into a subdirectory; "artifacts/" prefixes are accepted
        renamed = client.post(
            f"{base}/rename",
            json={"source": "artifacts/a.txt", "target": "notes/alpha.txt"},
        )
        assert renamed.json()["path"] == "artifacts/notes/alpha.txt"
        listed = client.get(base).json()["artifacts"]
        assert [a["path"] for a in listed] == ["b.txt", "notes/alpha.txt"]
        served = client.get(f"{base}/notes/alpha.txt")
        assert served.status_code == 200 and served.content == b"alpha"

        # never overwrites; refuses traversal and missing sources
        client.post(f"{base}?name=a.txt", content=b"other")
        taken = client.post(
            f"{base}/rename", json={"source": "a.txt", "target": "b.txt"}
        )
        assert taken.status_code == 409
        assert (
            client.post(
                f"{base}/rename", json={"source": "b.txt", "target": "../out.txt"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"{base}/rename", json={"source": "nope.txt", "target": "x.txt"}
            ).status_code
            == 404
        )


class TestSources:
    def test_upload_serves_and_records_the_source_file(self, tmp_path, monkeypatch):
        from reasoning_ledger import service

        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        store = LedgerStore(tmp_path / "workbench" / "projects" / project / "ledger")
        ref = service.reference_payload(store, author="A", title="Paywalled Paper")
        rid = ref["reference_id"]

        posted = client.post(
            f"/api/projects/{project}/references/{rid}/file?name=paper.pdf",
            content=b"%PDF-1.4 cached copy",
        )
        assert posted.status_code == 200
        assert posted.json()["file"] == f"{rid}/paper.pdf"

        served = client.get(f"/api/projects/{project}/sources/{rid}/paper.pdf")
        assert served.status_code == 200
        assert served.content == b"%PDF-1.4 cached copy"
        # the ledger's reference now carries the path
        refs = service.references_payload(store)["references"]
        assert refs[0]["file"] == f"{rid}/paper.pdf"

    def test_upload_to_unknown_reference_leaves_nothing(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        ghost = "ref_" + "0" * 24
        posted = client.post(
            f"/api/projects/{project}/references/{ghost}/file?name=x.pdf",
            content=b"data",
        )
        assert posted.status_code == 404
        assert not (
            tmp_path / "workbench" / "projects" / project / "sources" / ghost / "x.pdf"
        ).exists()

    def test_source_paths_cannot_escape_the_directory(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        secret = tmp_path / "workbench" / "projects" / project / "project.json"
        assert secret.exists()
        escape = client.get(f"/api/projects/{project}/sources/..%2Fproject.json")
        assert escape.status_code == 404


class TestProjects:
    def test_project_and_chat_lifecycle(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        assert client.get("/api/projects").json() == {"projects": []}

        project = client.post("/api/projects", json={"name": "Habiro π₀"}).json()
        assert project["id"] == "habiro"
        chat = client.post(f"/api/projects/{project['id']}/chats", json={}).json()
        assert chat["title"] == "chat 1"

        listing = client.get("/api/projects").json()["projects"]
        assert [p["id"] for p in listing] == ["habiro"]
        assert [c["id"] for c in listing[0]["chats"]] == [chat["id"]]

        history = client.get(f"/api/projects/{project['id']}/chats/{chat['id']}/history").json()
        assert history["messages"] == []
        assert isinstance(history["seq"], int)
        assert history["thread_id"] is None
        assert history["effort"] == "xhigh"
        assert history["turn_active"] is False
        assert "max" in history["efforts"]

        assert client.get("/api/projects/nope/ledger/status").status_code == 404
        assert client.get(f"/api/projects/{project['id']}/chats/nope/history").status_code == 404

    def test_project_ledgers_are_isolated(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        first = client.post("/api/projects", json={"name": "one"}).json()["id"]
        second = client.post("/api/projects", json={"name": "two"}).json()["id"]
        root = tmp_path / "workbench" / "projects"
        LedgerStore(root / first / "ledger").create_claim("Only in one.", author=AUTHOR)

        one = client.get(f"/api/projects/{first}/ledger/status").json()
        two = client.get(f"/api/projects/{second}/ledger/status").json()
        assert len(one["claims"]) == 1
        assert two["claims"] == []




    def test_projects_keep_user_order_when_pinned(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        older = client.post("/api/projects", json={"name": "Older"}).json()["id"]
        newer = client.post("/api/projects", json={"name": "Newer"}).json()["id"]
        root = tmp_path / "workbench" / "projects"
        for project_id, created_at in (
            (older, "2026-01-01T00:00:00+00:00"),
            (newer, "2026-02-01T00:00:00+00:00"),
        ):
            path = root / project_id / "project.json"
            meta = json.loads(path.read_text(encoding="utf-8"))
            meta["created_at"] = created_at
            path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        client.patch(f"/api/projects/{older}", json={"name": "Older, active"})
        listing = client.get("/api/projects").json()["projects"]
        assert [project["id"] for project in listing] == [newer, older]
        assert [project["pinned"] for project in listing] == [False, False]

        pinned = client.patch(f"/api/projects/{older}", json={"pinned": True}).json()
        assert pinned["pinned"] is True
        listing = client.get("/api/projects").json()["projects"]
        assert [project["id"] for project in listing] == [newer, older]

        client.patch(f"/api/projects/{older}", json={"archived": True})
        archived = next(
            project
            for project in client.get("/api/projects?include_archived=true").json()["projects"]
            if project["id"] == older
        )
        assert archived["id"] == older
        assert archived["pinned"] is False

    def test_chats_keep_user_order_when_pinned(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "Research"}).json()["id"]
        older = client.post(f"/api/projects/{project}/chats", json={"title": "Older"}).json()["id"]
        newer = client.post(f"/api/projects/{project}/chats", json={"title": "Newer"}).json()["id"]
        for chat_id, created_at in (
            (older, "2026-01-01T00:00:00+00:00"),
            (newer, "2026-02-01T00:00:00+00:00"),
        ):
            path = tmp_path / "workbench" / "projects" / project / "chats" / chat_id / "chat.json"
            meta = json.loads(path.read_text(encoding="utf-8"))
            meta["created_at"] = created_at
            path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        client.patch(
            f"/api/projects/{project}/chats/{older}",
            json={"title": "Older, active"},
        )
        chats = client.get("/api/projects").json()["projects"][0]["chats"]
        assert [chat["id"] for chat in chats] == [older, newer]

        pinned = client.patch(f"/api/projects/{project}/chats/{newer}", json={"pinned": True}).json()
        assert pinned["pinned"] is True
        chats = client.get("/api/projects").json()["projects"][0]["chats"]
        assert [chat["id"] for chat in chats] == [older, newer]

        archived = client.patch(f"/api/projects/{project}/chats/{newer}", json={"archived": True}).json()
        assert archived["archived"] is True and archived["pinned"] is False
        active = client.get("/api/projects").json()["projects"][0]["chats"]
        assert [chat["id"] for chat in active] == [older]

    def test_workflow_management_matches_chat_management(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "Research"}).json()["id"]
        older = client.post(f"/api/projects/{project}/workflows", json={"name": "Older"}).json()
        newer = client.post(f"/api/projects/{project}/workflows", json={"name": "Newer"}).json()

        pinned = client.patch(
            f"/api/projects/{project}/workflows/{older['workflow_id']}",
            json={"pinned": True},
        ).json()
        assert pinned["pinned"] is True
        workflows = client.get("/api/projects").json()["projects"][0]["workflows"]
        assert [workflow["workflow_id"] for workflow in workflows] == [
            older["workflow_id"],
            newer["workflow_id"],
        ]

        renamed = client.patch(
            f"/api/projects/{project}/workflows/{older['workflow_id']}",
            json={"name": "Older, renamed"},
        ).json()
        assert renamed["name"] == "Older, renamed"
        graph = client.get(f"/api/projects/{project}/workflows/{older['workflow_id']}").json()
        assert graph["name"] == "Older, renamed"

        archived = client.patch(
            f"/api/projects/{project}/workflows/{older['workflow_id']}",
            json={"archived": True},
        ).json()
        assert archived["archived"] is True and archived["pinned"] is False
        active = client.get(f"/api/projects/{project}/workflows").json()["workflows"]
        assert [workflow["workflow_id"] for workflow in active] == [newer["workflow_id"]]
        all_workflows = client.get(f"/api/projects/{project}/workflows?include_archived=true").json()["workflows"]
        assert {workflow["workflow_id"] for workflow in all_workflows} == {
            older["workflow_id"],
            newer["workflow_id"],
        }

        deleted = client.delete(f"/api/projects/{project}/workflows/{newer['workflow_id']}").json()
        assert deleted["workflow_id"] == newer["workflow_id"]
        assert "deleted_at" in deleted

    def test_project_and_chat_management_is_recoverable(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "Original"}).json()
        chat = client.post(f"/api/projects/{project['id']}/chats", json={"title": "Draft"}).json()

        renamed = client.patch(f"/api/projects/{project['id']}", json={"name": "Renamed"}).json()
        assert renamed["name"] == "Renamed"
        chat_renamed = client.patch(
            f"/api/projects/{project['id']}/chats/{chat['id']}",
            json={"title": "Week one", "archived": True},
        ).json()
        assert chat_renamed["title"] == "Week one"
        assert client.get("/api/projects").json()["projects"][0]["chats"] == []
        archived_listing = client.get("/api/projects?include_archived=true").json()["projects"]
        assert archived_listing[0]["chats"][0]["archived"] is True

        client.patch(f"/api/projects/{project['id']}", json={"archived": True})
        assert client.get("/api/projects").json() == {"projects": []}
        assert client.get("/api/projects?include_archived=true").json()["projects"][0]["archived"] is True

        deleted_chat = client.delete(f"/api/projects/{project['id']}/chats/{chat['id']}").json()
        assert deleted_chat["recoverable"] is True
        project_root = tmp_path / "workbench" / "projects" / project["id"]
        assert any((project_root / "chats" / ".trash").iterdir())

        deleted_project = client.delete(f"/api/projects/{project['id']}").json()
        assert deleted_project["recoverable"] is True
        assert not project_root.exists()
        assert any((tmp_path / "workbench" / ".trash").iterdir())

    def test_development_workspace_is_an_explicit_chat_grant(self, tmp_path, monkeypatch):
        repository = tmp_path / "reasoning-source"
        repository.mkdir()
        (repository / ".git").mkdir()
        monkeypatch.delenv("REASONING_APPROVALS", raising=False)
        monkeypatch.setattr(ChatSession, "_command", lambda self: list(FAKE))
        client = _client(tmp_path, monkeypatch)

        registered = client.post(
            "/api/settings/development-workspaces",
            json={"name": "Reasoning", "path": str(repository)},
        )
        assert registered.status_code == 200
        workspace = registered.json()["workspace"]
        assert workspace["available"] is True

        project = client.post("/api/projects", json={"name": "Interface"}).json()
        research = client.post(f"/api/projects/{project['id']}/chats", json={}).json()
        development = client.post(
            f"/api/projects/{project['id']}/chats",
            json={"title": "Improve Reasoning", "workspace_id": workspace["workspace_id"]},
        ).json()
        assert research["mode"] == "research" and research["workspace"] is None
        assert development["mode"] == "development"
        assert development["workspace"]["path"] == str(repository.resolve())
        rejected = client.post(
            f"/api/projects/{project['id']}/chats/{development['id']}/messages",
            json={"message": "edit the UI", "executor_id": "hermes"},
        )
        assert rejected.status_code == 422
        assert "development chats use Codex" in rejected.json()["detail"]

        listing = client.get("/api/projects?include_archived=true").json()["projects"]
        listed = next(item for item in listing[0]["chats"] if item["id"] == development["id"])
        assert listed["workspace"]["name"] == "Reasoning"

        # The explicit development grant must surface repository escalation
        # prompts even if a desktop or web launcher omitted the global flag.
        base = f"/api/projects/{project['id']}/chats/{development['id']}"
        started = client.post(f"{base}/messages", json={"message": "approve"}).json()
        assert started["started"] is True
        assert re.fullmatch(r"op_[0-9a-f]{32}", started["operation_id"])
        deadline = time.time() + 10
        pending = []
        while time.time() < deadline:
            history = client.get(f"{base}/history").json()
            pending = history["pending_approvals"]
            if pending:
                break
        assert pending and pending[0]["command"] == "rm -rf /"
        assert client.post(
            f"{base}/approvals",
            json={"request_id": pending[0]["request_id"], "decision": "decline"},
        ).json() == {"resolved": True}
        while time.time() < deadline:
            if not client.get(f"{base}/history").json()["turn_active"]:
                break
        assert not client.get(f"{base}/history").json()["turn_active"]

        in_use = client.delete(f"/api/settings/development-workspaces/{workspace['workspace_id']}")
        assert in_use.status_code == 409
        client.delete(f"/api/projects/{project['id']}/chats/{development['id']}")
        removed = client.delete(f"/api/settings/development-workspaces/{workspace['workspace_id']}")
        assert removed.status_code == 200
        assert repository.is_dir()


    def test_development_workspace_environment_setup_is_private(self, tmp_path, monkeypatch):
        repository = tmp_path / "native-source"
        repository.mkdir()
        (repository / ".git").mkdir()
        marker = repository / ".ready"
        client = _client(tmp_path, monkeypatch)
        workspace = client.post(
            "/api/settings/development-workspaces",
            json={"name": "Native", "path": str(repository)},
        ).json()["workspace"]
        base = f"/api/settings/development-workspaces/{workspace['workspace_id']}/environment"

        assert workspace["workspace_environment"]["managed"] is False
        configured = client.patch(
            base,
            json={
                "label": "Native environment",
                "check_command": shlex.join(
                    [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; raise SystemExit(not Path({str(marker)!r}).exists())",
                    ]
                ),
                "setup_command": shlex.join(
                    [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).write_text('ready')",
                    ]
                ),
            },
        )
        assert configured.status_code == 200
        workspace = configured.json()["workspace"]
        assert workspace["workspace_environment"]["state"] == "setup_required"
        assert not (repository / ".reasoning").exists()
        failed = client.post(f"{base}/check")
        assert failed.status_code == 200
        assert failed.json()["workspace"]["workspace_environment"]["ready"] is False

        setup = client.post(f"{base}/setup")
        assert setup.status_code == 200
        assert setup.json()["workspace"]["workspace_environment"]["state"] == "ready"
        assert marker.read_text(encoding="utf-8") == "ready"

        cleared = client.delete(base)
        assert cleared.status_code == 200
        assert cleared.json()["workspace"]["workspace_environment"]["managed"] is False

    def test_invalid_development_workspace_does_not_create_chat(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "Interface"}).json()
        response = client.post(
            f"/api/projects/{project['id']}/chats",
            json={"workspace_id": "missing"},
        )
        assert response.status_code == 404
        chats_root = tmp_path / "workbench" / "projects" / project["id"] / "chats"
        assert list(chats_root.glob("*/chat.json")) == []

    def test_workbench_root_defaults_to_xdg_data_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("REASONING_WORKBENCH_ROOT", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        client = TestClient(build_app())
        assert client.get("/api/projects").json() == {"projects": []}
        client.post("/api/projects", json={"name": "p"})
        assert (tmp_path / "xdg" / "reasoning" / "projects" / "p").is_dir()

    def test_artifacts_are_served_within_their_directory(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        artifacts = tmp_path / "workbench" / "projects" / project / "artifacts"
        (artifacts / "notes").mkdir(parents=True)
        (artifacts / "notes" / "drafts").mkdir()
        (artifacts / "notes" / "audit.md").write_text("# audit\n", encoding="utf-8")
        (artifacts / "notes" / "scan one.pdf").write_bytes(b"%PDF-1.7\n%%EOF\n")
        (artifacts / "notes" / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (tmp_path / "secret.txt").write_text("no", encoding="utf-8")

        good = client.get(f"/api/projects/{project}/artifacts/notes/audit.md")
        assert good.status_code == 200
        assert good.text == "# audit\n"

        pdf = client.get(f"/api/projects/{project}/artifacts/notes/scan%20one.pdf")
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.headers["content-disposition"].startswith("inline;")

        image = client.get(f"/api/projects/{project}/artifacts/notes/figure.png")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.headers["content-disposition"].startswith("inline;")

        listing = client.get(f"/api/projects/{project}/artifacts", params={"path": "notes"})
        assert listing.status_code == 200
        payload = listing.json()
        assert {"path": payload["path"], "entries": payload["entries"]} == {
            "path": "notes/",
            "entries": [
                {
                    "name": "drafts",
                    "path": "notes/drafts/",
                    "kind": "directory",
                    "bytes": None,
                },
                {
                    "name": "audit.md",
                    "path": "notes/audit.md",
                    "kind": "file",
                    "bytes": 8,
                },
                {
                    "name": "figure.png",
                    "path": "notes/figure.png",
                    "kind": "file",
                    "bytes": 8,
                },
                {
                    "name": "scan one.pdf",
                    "path": "notes/scan one.pdf",
                    "kind": "file",
                    "bytes": 15,
                },
            ],
        }
        assert [artifact["path"] for artifact in payload["artifacts"]] == [
            "notes/audit.md",
            "notes/figure.png",
            "notes/scan one.pdf",
        ]
        assert client.get(f"/api/projects/{project}/artifacts", params={"path": "notes/audit.md"}).status_code == 404
        assert client.get(f"/api/projects/{project}/artifacts", params={"path": "../../../"}).status_code == 404

        edited = client.put(
            f"/api/projects/{project}/artifacts/notes/audit.md",
            content="# revised\n",
        )
        assert edited.status_code == 200
        assert edited.json() == {"path": "notes/audit.md", "bytes": 10}
        assert (artifacts / "notes" / "audit.md").read_text(encoding="utf-8") == ("# revised\n")
        assert client.put(f"/api/projects/{project}/artifacts/notes/figure.png", content=b"text").status_code == 415
        assert client.put(f"/api/projects/{project}/artifacts/notes/missing.md", content="text").status_code == 404
        assert (
            client.put(
                f"/api/projects/{project}/artifacts/..%2F..%2F..%2Fsecret.txt",
                content="leak",
            ).status_code
            == 404
        )

        assert client.get(f"/api/projects/{project}/artifacts/missing.md").status_code == 404
        traversal = client.get(f"/api/projects/{project}/artifacts/..%2F..%2F..%2Fsecret.txt")
        assert traversal.status_code == 404

    def test_effort_and_decision_validation(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        bad = client.post(
            f"/api/projects/{project}/chats/{chat}/messages",
            json={"message": "hi", "effort": "extreme"},
        )
        assert bad.status_code == 422
        bad = client.post(
            f"/api/projects/{project}/chats/{chat}/approvals",
            json={"request_id": "x", "decision": "maybe"},
        )
        assert bad.status_code == 422

        bad = client.post(
            f"/api/projects/{project}/chats/{chat}/messages",
            json={"message": "hi", "model_provider": "not a provider"},
        )
        assert bad.status_code == 422


class TestCommand:
    def test_ledger_cli_prefers_the_service_environment(self, tmp_path, monkeypatch):
        runtime = tmp_path / "venv" / "bin"
        runtime.mkdir(parents=True)
        python = runtime / "python"
        bundled = runtime / "reasoning-ledger"
        bundled.write_text("", encoding="utf-8")
        shadow = tmp_path / "user-bin" / "reasoning-ledger"
        monkeypatch.setattr("reasoning_ledger.webapp.sys.executable", str(python))
        monkeypatch.setattr("reasoning_ledger.webapp.shutil.which", lambda name: str(shadow))

        assert _session(tmp_path)._ledger_cli() == str(bundled)

    def test_chat_project_tools_include_scoped_library_reader(self, tmp_path):
        session = _session(
            tmp_path,
            bibliography_root=tmp_path / "library",
            project_id="project-a",
            chat_id="chat-1",
            control_url="http://127.0.0.1:43119",
        )
        joined = " ".join(session._command())
        assert "bibliography_get" in joined
        assert "bibliography_resource_read" in joined
        assert "bibliography_summary_add" in joined
        assert "--chat-id" in joined
        assert "chat-1" in joined
        assert "--control-url" in joined
        assert "http://127.0.0.1:43119" in joined
        for tool_name in (
            "prompt_list",
            "prompt_read",
            "prompt_create",
            "prompt_revise",
            "workflow_list",
            "workflow_read",
            "workflow_create",
            "workflow_update",
            "workflow_preflight",
            "workflow_start",
        ):
            assert tool_name in joined
        ledger_env = session._ledger_env()
        assert ledger_env["REASONING_BIBLIOGRAPHY_ROOT"] == str((tmp_path / "library").resolve())
        assert ledger_env["REASONING_PROJECT_ID"] == "project-a"
        assert ledger_env["REASONING_CHAT_ID"] == "chat-1"

    def test_chat_project_tools_include_noether_tools(
        self,
        tmp_path,
        monkeypatch,
    ):
        manifest = tmp_path / "noether" / "installation.json"
        monkeypatch.setenv("REASONING_NOETHER_MANIFEST", str(manifest))

        joined = " ".join(_session(tmp_path)._command())

        assert "noether_status" in joined
        assert "noether_describe" in joined
        assert "noether_compute" in joined

    def test_appserver_command_shape(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REASONING_SANDBOX", "off")  # inspect the raw codex line
        session = _session(tmp_path)
        session._codex_command = None
        try:
            command = session._command()
        except Exception:
            pytest.skip("codex not on PATH")
        joined = " ".join(command)
        assert command[1] == "app-server"
        assert 'mcp_servers.ledger.args=["mcp"]' in joined
        assert "mcp_servers.project.command" in joined
        for tool_name in {
            "workflow_run_list",
            "workflow_run_read",
            "crossref_search",
            "crossref_lookup",
            "arxiv_search",
            "arxiv_lookup",
            "arxiv_read",
            "arxiv_source_read",
            "oeis_bfile",
            "lmfdb_number_fields",
            "lmfdb_elliptic_curves_q",
            "lean_environment",
            "lean_check",
            "scientific_calculate",
            "unit_convert",
            "constant_lookup",
            "artifact_write",
        }:
            assert tool_name in joined
        assert 'REASONING_LEDGER_SESSION="proj/chat-1"' in joined
        assert "tools.web_search=true" in joined
        assert 'model_reasoning_summary="detailed"' in joined
        assert "multi_agent_v2" in joined
        assert "guardian_approval" in joined
        assert "--disable apps" in joined
        assert "sandbox_workspace_write.writable_roots" in joined
        assert "sandbox_workspace_write.network_access=true" in joined

    def test_appserver_command_is_sandboxed_when_available(self, tmp_path, monkeypatch):
        from reasoning_ledger import sandbox

        if not sandbox.available():
            pytest.skip("bwrap unavailable")
        monkeypatch.setenv("REASONING_SANDBOX", "bwrap")
        session = _session(tmp_path)
        session._codex_command = None
        try:
            command = session._command()
        except Exception:
            pytest.skip("codex not on PATH")
        assert command[0] == "bwrap"
        joined = " ".join(command)
        # this project's ledger is bound; the app-server flags still ride along
        assert str(session.ledger_root) in joined
        assert "app-server" in command
        assert 'mcp_servers.ledger.args=["mcp"]' in joined

    def test_appserver_sandbox_exposes_external_python_runtime(self, tmp_path, monkeypatch):
        from reasoning_ledger import sandbox

        if not sandbox.available():
            pytest.skip("bwrap unavailable")
        monkeypatch.setenv("REASONING_SANDBOX", "bwrap")
        python_runtime = tmp_path / "standalone-python"
        (python_runtime / "bin").mkdir(parents=True)
        python_alias = tmp_path / "python-stable"
        python_alias.symlink_to(python_runtime, target_is_directory=True)
        venv = tmp_path / "external-venv"
        (venv / "bin").mkdir(parents=True)
        python_launcher = venv / "bin" / "python"
        python_launcher.symlink_to(python_alias / "bin" / "python3")
        monkeypatch.setattr(sys, "executable", str(python_launcher))
        monkeypatch.setattr(sys, "base_prefix", str(python_runtime))
        session = _session(tmp_path)
        session._codex_command = None
        try:
            command = session._command()
        except Exception:
            pytest.skip("codex not on PATH")

        index = command.index(str(python_runtime))
        assert command[index - 1] == "--ro-bind-try"
        alias_index = command.index(str(python_alias))
        assert command[alias_index - 1] == "--ro-bind-try"

    def test_sandbox_off_leaves_command_unwrapped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REASONING_SANDBOX", "off")
        session = _session(tmp_path)
        session._codex_command = None
        try:
            command = session._command()
        except Exception:
            pytest.skip("codex not on PATH")
        assert command[0] != "bwrap"

    def test_outer_namespace_replaces_nested_codex_sandbox(self, tmp_path, monkeypatch):
        from reasoning_ledger import sandbox as sandbox_module

        session = _session(tmp_path)
        assert session._codex_sandbox_mode() == "workspace-write"
        session._codex_command = None
        monkeypatch.setattr(sandbox_module, "enabled", lambda: True)
        monkeypatch.setattr(sandbox_module, "available", lambda: True)
        assert session._codex_sandbox_mode() == "danger-full-access"
        monkeypatch.setattr(sandbox_module, "available", lambda: False)
        assert session._codex_sandbox_mode() == "workspace-write"

    def test_self_development_does_not_overlay_repository_source_read_only(self, tmp_path, monkeypatch):
        from reasoning_ledger import sandbox as sandbox_module

        repository = Path(__file__).resolve().parents[1]
        state = tmp_path / "state"
        state.mkdir()
        captured = {}

        def capture(command, *, rw, ro, chdir):
            captured.update({"rw": rw, "ro": ro, "chdir": chdir})
            return command

        session = ChatSession(
            repository,
            tmp_path / "ledger",
            "gpt-5.6-sol",
            "workspace-write",
            state_dir=state,
            development_workspace={"name": "Reasoning"},
        )
        monkeypatch.setattr(ChatSession, "_ledger_cli", lambda self: "/bin/true")
        monkeypatch.setattr("reasoning_ledger.webapp.shutil.which", lambda name: "/bin/true")
        monkeypatch.setattr(sandbox_module, "wrap", capture)

        session._command()

        assert repository in captured["rw"]
        assert captured["chdir"] == repository
        assert all(not path.absolute().is_relative_to(repository) for path in captured["ro"])
        assert "docs/design-language.md" in session._developer_instructions()

    def test_developer_instructions_carry_ledger_contract(self, tmp_path):
        session = _session(tmp_path)
        text = session._developer_instructions()
        assert "ledger_status" in text
        assert str((tmp_path / "artifacts").resolve()) in text
        assert "workflow_run_list" in text
        assert "Workflow runs are saved project artifacts" in text
        assert "reviewable draft" in text
        assert "explicitly says to run or start" in text
        assert "immutable role/task prompts" in text
        assert "config.effort" in text
        assert "every useful file its own Markdown link" in text
        assert "with a trailing slash" in text
        assert "MCP server named `project`" in text
        assert "as tools, not MCP resources" in text
        assert "do not use resource discovery" in text
        assert "do not bypass their version checks" in text

    def test_research_command_disables_general_shell_tools(self, tmp_path):
        session = _session(tmp_path)
        command = session._command()
        assert command.count("--disable") >= 3
        assert "shell_tool" in command
        assert "unified_exec" in command

        development = _session(
            tmp_path,
            development_workspace={
                "workspace_id": "reasoning",
                "name": "Reasoning",
                "path": str(tmp_path / "repository"),
            },
        )
        development_command = development._command()
        assert "shell_tool" not in development_command
        assert "unified_exec" not in development_command

    def test_development_instructions_identify_repository_and_git_safety(self, tmp_path):
        session = _session(
            tmp_path,
            development_workspace={
                "workspace_id": "reasoning",
                "name": "Reasoning",
                "path": str(tmp_path / "scratch"),
            },
        )
        text = session._developer_instructions()
        assert "explicitly selected development chat" in text
        assert str((tmp_path / "scratch").resolve()) in text
        assert "Preserve unrelated user changes" in text
        assert "Do not change branches, commit, push" in text

    def test_workflow_runs_enter_the_hidden_project_surroundings(self, tmp_path):
        session = _session(tmp_path)
        runs = tmp_path / "artifacts" / "workflow-runs"
        runs.mkdir(parents=True)
        (runs / "wfrun_weekly.json").write_text(
            json.dumps(
                {
                    "run_id": "wfrun_weekly",
                    "workflow_id": "wf_weekly",
                    "workflow_snapshot": {"name": "Weekly advisor"},
                    "status": "completed",
                    "iterations": 50,
                    "cost": {"total_usd": 1.25},
                    "artifact_path": "workflow-runs/wfrun_weekly.md",
                    "updated_at": "2026-08-06T12:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        (runs / "wfrun_weekly.md").write_text("# Weekly advisor\n", encoding="utf-8")
        context = session._project_overview()
        assert context is not None
        assert "Weekly advisor" in context
        assert "wfrun_weekly" in context
        assert "workflow-runs/wfrun_weekly.md" in context
        assert "distinct from the claim ledger" in context


class TestTurns:
    def test_codex_turn_inherits_saved_provider_credentials_without_cli_override(self, tmp_path):
        secret = "sk-or-chat-process-only"
        session = _session(
            tmp_path,
            credential_environment=lambda: {"OPENROUTER_API_KEY": secret},
        )
        command = " ".join(session._command())
        assert "model_provider" not in command
        assert "wire_api" not in command
        assert session._client_environment()["OPENROUTER_API_KEY"] == secret

        _, subscriber = session.subscribe()
        assert session.start_turn(
            "quick",
            "low",
            "provider/model",
            executor_id="codex",
        ) == "started"
        events = _collect(subscriber)
        session.unsubscribe(subscriber)
        assert events[0]["executor_id"] == "codex"
        assert events[-1]["executor_id"] == "codex"
        messages = session.transcript()
        assert messages[0]["executor_id"] == "codex"
        assert messages[1]["executor_id"] == "codex"
        assert secret not in json.dumps(messages)
        session.close()

    def test_codex_turn_records_explicit_provider(self, tmp_path):
        session = _session(tmp_path)
        _, subscriber = session.subscribe()
        assert (
            session.start_turn(
                "quick",
                "low",
                "stealth/ox-alpha",
                executor_id="codex",
                model_provider="openrouter",
            )
            == "started"
        )
        events = _collect(subscriber)
        session.unsubscribe(subscriber)

        assert events[0]["model_provider"] == "openrouter"
        assert events[-1]["model_provider"] == "openrouter"
        messages = session.transcript()
        assert messages[0]["model_provider"] == "openrouter"
        assert messages[1]["model_provider"] == "openrouter"
        assert session.thread_provider == "openrouter"
        session.close()

    def test_quick_turn_broadcasts_and_persists(self, tmp_path):
        session = _session(tmp_path)
        events = _run(session, "quick", "low")
        types = [e["type"] for e in events]
        assert types[0] == "user_message"
        assert "turn.started" in types
        assert "thread.started" in types
        assert types[-1] == "turn.completed"
        hello = [
            e
            for e in events
            if e["type"] == "item.completed" and e["item"].get("type") == "agent_message" and e["item"].get("text") == "hello"
        ]
        assert hello
        roles = [e["role"] for e in session.transcript()]
        assert roles == ["user", "assistant"]
        assert session.thread_id is not None
        operation_id = session.last_operation_id
        assert operation_id is not None
        assert session.operation(operation_id)["side_effect_safety"] == {
            "effect_state": "none_observed",
            "event_stream_complete": True,
            "last_event_sequence": 3,
            "last_effect_sequence": None,
            "advertised_checkpoint_sequences": [],
            "effect_kinds": [],
        }
        checkpoint = session.export_portable_checkpoint(operation_id)
        assert checkpoint["automatic_failover"]["allowed"] is True
        assert [message["role"] for message in checkpoint["messages"]] == [
            "user",
            "assistant",
        ]
        session.close()

    def test_first_turn_is_seeded_with_the_ledger_overview(self, tmp_path):
        # A fresh thread gets the ledger overview injected (transient —
        # compaction can reclaim it), NOT as turn input (a user message
        # would be preserved verbatim forever). The fake marks items
        # injected while idle with [pre-injected]; later turns in the
        # same thread are not re-seeded.
        session = _session(tmp_path)
        events = _run(session, "say something")
        echoes = [
            e["item"]["text"]
            for e in events
            if e["type"] == "item.completed"
            and e["item"]["type"] == "agent_message"
        ]
        assert echoes == ["echo:[pre-injected]say something"]
        # the overview never reaches the visible transcript
        assert [e["role"] for e in session.transcript()] == ["user", "assistant"]
        assert session.transcript()[0]["text"] == "say something"
        # the observation record was pre-written with the delivered map
        assert session.observations_path.exists()
        events = _run(session, "again please")
        echoes = [e["item"]["text"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"]
        assert echoes == ["echo:again please"]
        session.close()

    def test_task_list_round_trips_and_seeds_later_turns(self, tmp_path):
        from reasoning_ledger.tasks import TaskStore

        session = _session(tmp_path)
        assert session.task_list() == []
        # the fresh chat's overview is injected, not turn input; a plain
        # second turn with no tasks carries no preamble at all
        _run(session, "first")
        events = _run(session, "second")
        echoes = [e["item"]["text"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"]
        assert echoes == ["echo:second"]
        # the agent writes tasks via the MCP tool (out-of-process); simulate
        TaskStore(session.tasks_path).set([{"title": "prove it", "status": "in_progress"}])
        assert session.task_list()[0]["title"] == "prove it"
        # now the task list rides the next turn's hidden preamble
        events = _run(session, "third")
        echoes = [e["item"]["text"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"]
        assert echoes == ["echo:[seeded]third"]
        # ...and never reaches the visible transcript
        assert [e["role"] for e in session.transcript()][-2:] == [
            "user",
            "assistant",
        ]
        session.close()

    def test_task_list_endpoint(self, tmp_path, monkeypatch):
        from reasoning_ledger.tasks import TaskStore

        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setattr("reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE))
        client = TestClient(build_app())
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        base = f"/api/projects/{project}/chats/{chat}"
        assert client.get(f"{base}/tasks").json() == {"tasks": [], "goal": None}
        TaskStore(
            tmp_path / "workbench" / "projects" / project / "chats" / chat / "agent-tasks" / "tasks.json"
        ).set([{"title": "step one", "status": "pending"}])
        assert client.get(f"{base}/tasks").json()["tasks"][0]["title"] == "step one"

    def test_turn_completes_with_no_subscribers(self, tmp_path):
        # The core multi-window guarantee: nobody watching, nothing lost.
        session = _session(tmp_path)
        assert session.start_turn("quick") == "started"
        _wait_turn_idle(session)
        assert [e["role"] for e in session.transcript()] == ["user", "assistant"]
        session.close()

    def test_subscribe_replays_buffered_events(self, tmp_path):
        session = _session(tmp_path)
        _run(session, "quick")
        # A window connecting after the fact replays the whole turn...
        replay, subscriber = session.subscribe(after=0)
        types = [e["type"] for e in replay]
        assert types[0] == "user_message"
        assert types[-1] == "turn.completed"
        seqs = [e["seq"] for e in replay]
        assert seqs == sorted(seqs)
        session.unsubscribe(subscriber)
        # ...and one holding the history snapshot's cursor replays nothing.
        _, seq = session.snapshot()
        replay, subscriber = session.subscribe(after=seq)
        assert replay == []
        session.unsubscribe(subscriber)
        session.close()

    def test_unsubscribe_is_safe_under_the_record_lock(self, tmp_path):
        # An abandoned event_stream generator is finalized by the GC,
        # which can run on a thread that already holds _record_lock (it
        # did, in production: the turn worker deadlocked against itself
        # inside _publish). unsubscribe must therefore never lock.
        session = _session(tmp_path)
        _, subscriber = session.subscribe()
        with session._record_lock:
            session.unsubscribe(subscriber)  # deadlocks if it takes the lock
        # the flagged subscriber is pruned by the next publish...
        session._publish({"type": "noop"})
        assert subscriber not in session._subscribers
        # ...and got nothing after unsubscribing.
        assert subscriber.empty()

    def test_thread_resumes_after_client_restart(self, tmp_path):
        session = _session(tmp_path)
        _run(session, "quick")
        first_thread = session.thread_id
        session.close()
        events = _run(session, "quick")
        assert session.thread_id == first_thread
        # resumed thread: no fresh thread.started event
        assert "thread.started" not in [e["type"] for e in events]
        session.close()

    def test_thread_start_persists_explicit_provider(self, tmp_path):
        session = _session(tmp_path)
        calls = []

        class Client:
            def request(self, method, params):
                calls.append((method, params))
                assert method == "thread/start"
                assert params["model"] == "stealth/ox-alpha"
                assert params["modelProvider"] == "openrouter"
                return {"thread": {"id": "thread_openrouter"}}

        assert (
            session._ensure_thread(
                Client(),
                "stealth/ox-alpha",
                model_provider="openrouter",
            )
            == "thread_openrouter"
        )
        assert [method for method, _ in calls] == ["thread/start"]
        assert json.loads(session.state_path.read_text(encoding="utf-8")) == {
            "thread_id": "thread_openrouter",
            "model_provider": "openrouter",
        }
        assert session.thread_provider == "openrouter"

    def test_thread_resume_uses_persisted_provider_without_reading_config(self, tmp_path):
        session = _session(tmp_path)
        session.state_path.write_text(
            '{"thread_id":"thread_old","model_provider":"openrouter"}\n',
            encoding="utf-8",
        )
        calls = []

        class Client:
            def request(self, method, params):
                calls.append((method, params))
                assert method == "thread/resume"
                assert params["threadId"] == "thread_old"
                assert params["modelProvider"] == "openrouter"
                return {"thread": {"id": "thread_old"}}

        assert (
            session._ensure_thread(
                Client(),
                "stealth/ox-alpha",
                model_provider="openrouter",
            )
            == "thread_old"
        )
        assert [method for method, _ in calls] == ["thread/resume"]

    def test_thread_rejects_provider_switch(self, tmp_path):
        session = _session(tmp_path)
        session.state_path.write_text(
            '{"thread_id":"thread_old","model_provider":"openrouter"}\n',
            encoding="utf-8",
        )

        class Client:
            def request(self, method, params):
                raise AssertionError(f"unexpected app-server request: {method} {params}")

        with pytest.raises(LedgerStoreError, match="bound to model provider openrouter"):
            session._ensure_thread(Client(), model_provider="openai")

    def test_legacy_thread_resume_uses_the_effective_current_provider(self, tmp_path):
        session = _session(tmp_path)
        session.state_path.write_text('{"thread_id":"thread_old"}\n', encoding="utf-8")
        calls = []

        class Client:
            def request(self, method, params):
                calls.append((method, params))
                if method == "config/read":
                    assert params == {"cwd": str(session.workdir)}
                    return {"config": {"model_provider": "replacement"}}
                assert method == "thread/resume"
                assert params["threadId"] == "thread_old"
                assert params["modelProvider"] == "replacement"
                return {"thread": {"id": "thread_old"}}

        assert session._ensure_thread(Client()) == "thread_old"
        assert [method for method, _ in calls] == ["config/read", "thread/resume"]

    def test_thread_state_is_atomically_replaced_and_corrupt_state_recovers(self, tmp_path, monkeypatch):
        session = _session(tmp_path)
        session.state_path.write_text('{"thread_id":"thread_old"}\n', encoding="utf-8")
        original_write = Path.write_text
        observed_during_temporary_write = []

        def observing_write(path, text, *args, **kwargs):
            if path.name.startswith(".chat-thread.json."):
                observed_during_temporary_write.append(session.thread_id)
            return original_write(path, text, *args, **kwargs)

        class Client:
            def request(self, method, params):
                if method == "config/read":
                    return {"config": {"model_provider": None}}
                assert method == "thread/resume"
                assert params["threadId"] == "thread_old"
                assert params["modelProvider"] == "openai"
                return {"thread": {"id": "thread_new"}}

        monkeypatch.setattr(Path, "write_text", observing_write)
        assert session._ensure_thread(Client()) == "thread_new"
        assert observed_during_temporary_write == ["thread_old"]
        assert session.thread_id == "thread_new"
        assert session.thread_provider == "openai"
        assert json.loads(session.state_path.read_text(encoding="utf-8")) == {
            "thread_id": "thread_new",
            "model_provider": "openai",
        }

        session._live_thread_id = None
        session.state_path.write_text("", encoding="utf-8")
        assert session.thread_id is None
        assert session.thread_provider is None

    def test_running_operation_becomes_interrupted_after_server_restart(self, tmp_path):
        session = _session(tmp_path)
        operation_id = "op_" + "a" * 32
        operation_path = session.operations_dir / f"{operation_id}.json"
        operation_path.write_text(
            json.dumps(
                {
                    "operation_id": operation_id,
                    "status": "running",
                    "created_at": "2026-08-22T00:00:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        recovered = _session(tmp_path)
        state = recovered.operation(operation_id)
        assert state["status"] == "interrupted"
        assert "server restarted" in state["error"]
        assert state["finished_at"]

    def test_invalid_effort_is_rejected(self, tmp_path):
        session = _session(tmp_path)
        with pytest.raises(LedgerStoreError, match="minimal"):
            session.start_turn("quick", "minimal")
        assert not session.turn_active

    def test_reasoning_deltas_and_usage(self, tmp_path):
        session = _session(tmp_path)
        events = _run(session, "reason")
        deltas = [{k: e[k] for k in ("type", "channel", "id", "text")} for e in events if e["type"] == "delta"]
        assert {"type": "delta", "channel": "reasoning", "id": "r1", "text": "thinking "} in deltas
        assert {"type": "delta", "channel": "message", "id": "m1", "text": "well "} in deltas
        usage = [e for e in events if e["type"] == "token_usage"]
        assert usage and usage[0]["usage"]["last"]["outputTokens"] == 10
        roles = [e["role"] for e in session.transcript()]
        assert roles == ["user", "reasoning", "assistant"]
        session.close()

    def test_mcp_elicitation_is_auto_accepted(self, tmp_path):
        session = _session(tmp_path)
        events = _run(session, "mcp")
        finals = [e["item"]["text"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"]
        assert finals == ["mcp-ok"]
        mcp = [e["item"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "mcp_tool_call"]
        assert mcp and mcp[0]["server"] == "reasoning"
        # tool use persists: history reloads rebuild the activity line
        transcript = session.transcript()
        assert [e["role"] for e in transcript] == ["user", "activity", "assistant"]
        assert transcript[1]["item"] == mcp[0]
        session.close()

    def test_subagent_activity_streams_from_child_thread(self, tmp_path):
        session = _session(tmp_path)
        events = _run(session, "spawn")
        sub = [e for e in events if e["type"] == "subagent"]
        kinds = [e["kind"] for e in sub]
        assert kinds == ["started", "tool", "message", "complete"]
        assert sub[0]["name"] == "reviewer"
        assert sub[1]["detail"] == "ledger.ledger_claim"
        assert sub[1]["arguments"] == {"statement": "S"}
        transcript = session.transcript()
        # recorded at completion time: between the user turn and the parent's
        # final message, preserving the inline position for history reloads
        assert [e["role"] for e in transcript] == ["user", "subagent", "assistant"]
        subagent_entry = {k: v for k, v in transcript[1].items() if k != "at"}
        assert re.fullmatch(r"op_[0-9a-f]{32}", subagent_entry.pop("operation_id"))
        assert subagent_entry == {
            "role": "subagent",
            "name": "reviewer",
            "text": "child report",
        }
        # usage aggregates across the thread tree: child (7 out) + parent
        # (10 out); `last` and the window stay the parent's own
        usage_events = [e for e in events if e["type"] == "token_usage"]
        assert usage_events
        final = usage_events[-1]["usage"]
        assert final["total"]["outputTokens"] == 17
        assert final["total"]["inputTokens"] == 40
        assert final["last"]["outputTokens"] == 10
        assert final["modelContextWindow"] == 1000
        session.close()


class TestLedgerPreflight:
    def test_broken_ledger_cli_surfaces_an_error(self, tmp_path, monkeypatch):
        # A ledger MCP server that cannot start (missing binary, missing
        # dependency) otherwise fails silently: codex just exposes no
        # ledger tools and the model shrugs. The preflight makes it loud.
        crasher = tmp_path / "broken-ledger"
        crasher.write_text("#!/bin/sh\necho 'RuntimeError: dep missing' >&2\nexit 1\n")
        crasher.chmod(0o755)
        monkeypatch.setattr(ChatSession, "_ledger_cli", lambda self: str(crasher))
        _ledger_preflight.clear()
        session = _session(tmp_path)
        events = _run(session, "quick")
        errors = [e for e in events if e["type"] == "error"]
        assert errors and "ledger" in errors[0]["message"]
        assert "dep missing" in errors[0]["message"]
        # the turn itself still ran to completion; the error is part
        # of the record now, so reloads keep showing it
        assert events[-1]["type"] == "turn.completed"
        assert [e["role"] for e in session.transcript()] == [
            "user",
            "error",
            "assistant",
        ]
        session.close()
        _ledger_preflight.clear()

    def test_missing_ledger_cli_surfaces_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ChatSession, "_ledger_cli", lambda self: str(tmp_path / "nope"))
        _ledger_preflight.clear()
        session = _session(tmp_path)
        events = _run(session, "quick")
        errors = [e for e in events if e["type"] == "error"]
        assert errors and "not found" in errors[0]["message"]
        session.close()
        _ledger_preflight.clear()

    def test_working_ledger_cli_stays_silent(self, tmp_path):
        # the real CLI (this venv) passes; result is cached process-wide
        session = _session(tmp_path)
        events = _run(session, "quick")
        assert not [e for e in events if e["type"] == "error"]
        assert session._ledger_problem() is None
        session.close()


class TestSteerAndInterrupt:
    def _start_wait_turn(self, session):
        _, subscriber = session.subscribe()
        assert session.start_turn("wait") == "started"
        # wait for the turn to actually start server-side
        deadline = time.time() + 10
        while time.time() < deadline and session._active_turn_id is None:
            time.sleep(0.02)
        assert session._active_turn_id is not None
        return subscriber

    def test_steer_reaches_running_turn(self, tmp_path):
        session = _session(tmp_path)
        subscriber = self._start_wait_turn(session)
        assert session.steer("new direction") == "steered"
        events = _collect(subscriber)
        steered = [e for e in events if e["type"] == "user_message"]
        # the wait-turn's own user message, then the steer
        assert [e.get("steered", False) for e in steered] == [False, True]
        finals = [e["item"]["text"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"]
        assert finals == ["steered:new direction"]
        transcript = session.transcript()
        # steer is recorded in stream order: before the reply it produced
        steer_entry = {k: v for k, v in transcript[-2].items() if k != "at"}
        assert re.fullmatch(r"op_[0-9a-f]{32}", steer_entry.pop("operation_id"))
        assert steer_entry == {
            "role": "user",
            "text": "new direction",
            "steered": True,
            "executor_id": "codex",
            "model": "gpt-5.6-sol",
            "model_provider": "openai",
        }
        reply = transcript[-1]
        assert reply["role"] == "assistant"
        assert reply["text"] == "steered:new direction"
        assert reply["message_id"].startswith("msg_")
        session.close()

    def test_steer_without_turn_returns_false(self, tmp_path):
        session = _session(tmp_path)
        assert session.steer("nothing running") == "no_turn"

    def test_interrupt_ends_turn(self, tmp_path):
        session = _session(tmp_path)
        subscriber = self._start_wait_turn(session)
        assert session.interrupt() is True
        events = _collect(subscriber)
        assert events[-1]["type"] == "turn.completed"
        assert events[-1]["status"] == "interrupted"
        _wait_turn_idle(session)
        session.close()


class TestApprovals:
    def test_default_auto_declines_escalations(self, tmp_path):
        session = _session(tmp_path)
        events = _run(session, "approve")
        assert not [e for e in events if e["type"] == "approval_request"]
        finals = [e["item"]["text"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"]
        assert finals == ["decision=decline"]
        denials = [e["item"]["command"] for e in events if e["type"] == "item.completed" and e["item"].get("type") == "command_execution"]
        assert any("auto-denied" in c and "rm -rf /" in c for c in denials)
        session.close()

    def test_approval_roundtrip(self, tmp_path):
        session = _session(tmp_path, interactive_approvals=True)
        _, subscriber = session.subscribe()
        assert session.start_turn("approve") == "started"
        events = _collect(subscriber, until="approval_request")
        request = events[-1]
        assert request["command"] == "rm -rf /"
        assert request["kind"] == "command"
        assert request["cwd"] == "/tmp"
        pending = session.pending_approvals()
        assert len(pending) == 1
        assert pending[0]["request_id"] == request["request_id"]
        assert pending[0]["expires_at"] > pending[0]["created_at"]
        assert session.resolve_approval(request["request_id"], "accept") is True
        assert session.pending_approvals() == []
        events += _collect(subscriber)
        resolved = [e for e in events if e["type"] == "approval_resolved"]
        assert resolved and resolved[0]["decision"] == "accept"
        assert resolved[0]["request_id"] == request["request_id"]
        finals = [e["item"]["text"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"]
        assert finals == ["decision=accept"]
        session.close()

    def test_unanswered_approval_times_out_to_decline(self, tmp_path):
        session = _session(tmp_path, interactive_approvals=True, approval_timeout=0.3)
        events = _run(session, "approve")
        finals = [e["item"]["text"] for e in events if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"]
        assert finals == ["decision=decline"]
        resolved = [e for e in events if e["type"] == "approval_resolved"]
        assert resolved and resolved[0]["decision"] == "decline"
        session.close()

    def test_unknown_approval_is_reported(self, tmp_path):
        session = _session(tmp_path)
        assert session.resolve_approval("apr_missing", "accept") is False


class TestMessageRepair:
    def test_broken_latex_message_is_repaired_and_swapped(self, tmp_path, monkeypatch):
        from reasoning_ledger import latex_check

        if not latex_check.available():
            pytest.skip("KaTeX checker unavailable")
        # a fake repair model that fixes \RR -> \mathbb{R}
        fake = tmp_path / "fake-repair"
        fake.write_text('#!/usr/bin/env python3\nimport sys, json\nsys.stdin.read()\nprint(json.dumps({"fixed": [r"\\mathbb{R}"]}))\n')
        fake.chmod(0o755)
        monkeypatch.setenv("REASONING_REPAIR_COMMAND", str(fake))

        session = _session(tmp_path)
        _, subscriber = session.subscribe()
        session.start_turn("brokentex")
        events = _collect(subscriber, until="text_repaired", timeout=20)
        swap = [e for e in events if e["type"] == "text_repaired"][0]
        assert swap["repaired"] == r"The ring \(\mathbb{R}\) is basic."
        assert swap["original"] == r"The ring \(\RR\) is basic."
        # the transcript entry now carries both versions, keyed by id
        _wait_turn_idle(session)
        assistant = [e for e in session.transcript() if e["role"] == "assistant"][0]
        assert assistant["latex_repaired"] == r"The ring \(\mathbb{R}\) is basic."
        assert assistant["latex_original"] == r"The ring \(\RR\) is basic."
        assert assistant["message_id"] == swap["id"]
        # served history carries the repair too, while the raw log was
        # never rewritten: the repair lives in its own sidecar
        served = [e for e in session.snapshot()[0] if e["role"] == "assistant"][0]
        assert served["latex_repaired"] == r"The ring \(\mathbb{R}\) is basic."
        assert b"latex_repaired" not in session.transcript_path.read_bytes()
        assert session.repairs_path.exists()
        session.close()

    def test_clean_message_is_not_repaired(self, tmp_path, monkeypatch):
        from reasoning_ledger import latex_check

        if not latex_check.available():
            pytest.skip("KaTeX checker unavailable")
        # repair model that would loudly change things if ever called
        fake = tmp_path / "fake-repair"
        fake.write_text('#!/usr/bin/env python3\nimport sys, json\nsys.stdin.read()\nprint(json.dumps({"fixed": ["WRONG"]}))\n')
        fake.chmod(0o755)
        monkeypatch.setenv("REASONING_REPAIR_COMMAND", str(fake))

        session = _session(tmp_path)
        events = _run(session, "quick")  # emits plain "hello", no math
        assert not [e for e in events if e["type"] == "text_repaired"]
        time.sleep(0.5)
        assistant = [e for e in session.transcript() if e["role"] == "assistant"][0]
        assert "latex_repaired" not in assistant
        session.close()


class TestEvidenceDisplay:
    def test_claim_detail_shows_evidence_repaired_when_present(self, tmp_path, monkeypatch):
        from reasoning_ledger import LedgerStore, service

        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setattr("reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE))
        client = TestClient(build_app())
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        store = LedgerStore(tmp_path / "workbench" / "projects" / project / "ledger")
        author = {"operator": "op", "agent": "a", "session": f"{project}/c"}
        claim = service.claim_payload(store, "A claim.", author=author)
        proof = service.prove_payload(store, claim["claim_id"], "Argument.", author=author)
        v = service.verify_payload(
            store,
            verdict="flawed",
            authority="review",
            evidence={"kind": "gap", "summary": r"uses \(\RR\) wrongly"},
            proof_id=proof["proof_id"],
            author=author,
        )
        base = f"/api/projects/{project}/ledger/claims/{claim['claim_id']}"

        # before any repair: original evidence, no `original` alternate
        vers = client.get(base).json()["verifications"]
        assert len(vers) == 1
        assert vers[0]["text"] == r"uses \(\RR\) wrongly"
        assert vers[0]["original"] is None
        assert vers[0]["verdict"] == "flawed"

        # the worker repairs it in place; the detail now serves repaired + original
        store.set_evidence_repair(claim["claim_id"], v["verification_id"], {"summary": r"uses \(\mathbb{R}\) wrongly"})
        vers = client.get(base).json()["verifications"]
        assert vers[0]["text"] == r"uses \(\mathbb{R}\) wrongly"
        assert vers[0]["original"] == r"uses \(\RR\) wrongly"

        # model-facing status_payload never carries evidence prose
        assert "verifications" not in service.status_payload(store, claim["claim_id"])


class TestReviews:
    def test_endpoint_and_announcer_land_reviews_in_the_chat(self, tmp_path, monkeypatch):
        from reasoning_ledger import review as review_module

        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setattr("reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE))
        client = TestClient(build_app())
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        ledger_root = tmp_path / "workbench" / "projects" / project / "ledger"

        review_module._ensure_reviews_dir(ledger_root)
        state = {
            "review_id": "rev_" + "a" * 16,
            "subject": {
                "claim_id": "clm_" + "1" * 24,
                "revision_id": "clmrev_" + "1" * 24,
            },
            "status": "completed",
            "verdict": "verified",
            "model": "test-model",
            "effort": "low",
            "requested_by": {
                "operator": "workbench",
                "agent": "m",
                "session": f"{project}/{chat}",
            },
            "started_at": "2026-01-01T00:00:00+00:00",
            "report": "obligations all hold",
            "announced": False,
        }
        review_module._write_state(ledger_root, state)

        listed = client.get(f"/api/projects/{project}/reviews").json()["reviews"]
        assert [r["review_id"] for r in listed] == [state["review_id"]]

        # the announcer thread (3s cadence) lands it in the requesting
        # chat's transcript exactly once
        deadline = time.time() + 15
        entries = []
        while time.time() < deadline:
            history = client.get(f"/api/projects/{project}/chats/{chat}/history").json()
            entries = [m for m in history["messages"] if m["role"] == "review"]
            if entries:
                break
            time.sleep(0.5)
        assert entries, "review never announced"
        assert entries[0]["verdict"] == "verified"
        assert entries[0]["review_id"] == state["review_id"]
        assert entries[0]["text"] == "obligations all hold"
        assert (
            review_module.load_review(ledger_root, state["review_id"])["announced"]
            is True
        )
        # once announced, the next tick settles it out of the announcer's
        # scan set; it stays fully readable from the settled directory
        deadline = time.time() + 15
        settled_path = review_module.settled_dir(ledger_root) / (
            state["review_id"] + ".json"
        )
        while time.time() < deadline and not settled_path.exists():
            time.sleep(0.5)
        assert settled_path.exists(), "review never settled"
        assert review_module.active_reviews(ledger_root) == []
        assert [
            r["review_id"] for r in review_module.load_reviews(ledger_root)
        ] == [state["review_id"]]
        assert (
            review_module.load_review(ledger_root, state["review_id"])["announced"]
            is True
        )

    def test_finished_review_is_injected_into_a_running_turn(self, tmp_path, monkeypatch):
        # The agent gets a callback: a review landing while the requesting
        # chat's turn is active is injected as a developer-role note, not
        # a user-role steer (the fake's "wait" turn completes by
        # acknowledging the injected note).
        from reasoning_ledger import review as review_module

        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setattr("reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE))
        client = TestClient(build_app())
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        base = f"/api/projects/{project}/chats/{chat}"
        ledger_root = tmp_path / "workbench" / "projects" / project / "ledger"

        assert client.post(f"{base}/messages", json={"message": "wait"}).status_code == 200
        deadline = time.time() + 10
        while time.time() < deadline:
            if client.get(f"{base}/history").json()["turn_active"]:
                break
            time.sleep(0.1)

        review_module._ensure_reviews_dir(ledger_root)
        review_module._write_state(
            ledger_root,
            {
                "review_id": "rev_" + "c" * 16,
                "subject": {
                    "claim_id": "clm_" + "3" * 24,
                    "proof_id": "prf_" + "3" * 24,
                },
                "status": "completed",
                "verdict": "flawed",
                "model": "test-model",
                "effort": "low",
                "requested_by": {"session": f"{project}/{chat}"},
                "started_at": "2026-01-01T00:00:00+00:00",
                "report": "gap in step 2",
                "announced": False,
            },
        )
        deadline = time.time() + 15
        reply = ""
        while time.time() < deadline:
            messages = client.get(f"{base}/history").json()["messages"]
            replies = [
                m["text"]
                for m in messages
                if m["role"] == "assistant" and "noted:" in m.get("text", "")
            ]
            if replies:
                reply = replies[0]
                break
            time.sleep(0.3)
        assert "independent review rev_cccccccccccccccc" in reply
        assert "flawed" in reply
        # the injected notification is context, not a user message
        assert not any(
            m["role"] == "user" and "independent review" in m.get("text", "") for m in client.get(f"{base}/history").json()["messages"]
        )

    def test_finished_review_wakes_an_idle_chat(self, tmp_path, monkeypatch):
        # No turn running: the announcer starts one so the model reacts to
        # the review instead of parking. The fake echoes its input, and no
        # user message is recorded for the wake.
        from reasoning_ledger import review as review_module

        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setattr("reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE))
        client = TestClient(build_app())
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        base = f"/api/projects/{project}/chats/{chat}"
        ledger_root = tmp_path / "workbench" / "projects" / project / "ledger"

        review_module._ensure_reviews_dir(ledger_root)
        review_module._write_state(
            ledger_root,
            {
                "review_id": "rev_" + "d" * 16,
                "subject": {"kind": "writeup"},
                "status": "completed",
                "verdict": None,
                "model": "test-model",
                "effort": "low",
                "requested_by": {"session": f"{project}/{chat}"},
                "started_at": "2026-01-01T00:00:00+00:00",
                "report": "reads well; one undefined symbol",
                "announced": False,
            },
        )
        deadline = time.time() + 15
        reply = ""
        while time.time() < deadline:
            messages = client.get(f"{base}/history").json()["messages"]
            replies = [m["text"] for m in messages if m["role"] == "assistant" and "echo:" in m.get("text", "")]
            if replies:
                reply = replies[0]
                break
            time.sleep(0.3)
        assert "writeup" in reply and "feedback ready" in reply
        # the wake never recorded a user message
        assert not any(m["role"] == "user" for m in client.get(f"{base}/history").json()["messages"])

    def test_watcher_spawns_pending_reviews(self, tmp_path, monkeypatch):
        # Reviews are requested from inside the codex sandbox (no codex on
        # PATH there), so request_review only records "pending"; the
        # workbench watcher spawns the runner from out here.
        from reasoning_ledger import review as review_module

        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setattr("reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE))
        client = TestClient(build_app())
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        ledger_root = tmp_path / "workbench" / "projects" / project / "ledger"
        review_module._ensure_reviews_dir(ledger_root)
        state = {
            "review_id": "rev_" + "b" * 16,
            "subject": {
                "claim_id": "clm_" + "2" * 24,
                "revision_id": "clmrev_" + "2" * 24,
            },
            "status": "pending",
            "model": "test-model",
            "effort": "low",
            "requested_by": None,
            "started_at": "2026-01-01T00:00:00+00:00",
            "announced": True,
        }
        review_module._write_state(ledger_root, state)
        # spawning marks it running; the runner then fails fast on the
        # unknown claim — either way it must leave "pending"
        current = state
        deadline = time.time() + 15
        while time.time() < deadline:
            current = review_module.load_review(ledger_root, state["review_id"])
            if current["status"] != "pending":
                break
            time.sleep(0.5)
        assert current["status"] != "pending"


class TestEndpoints:
    def _workbench(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setattr("reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE))
        app = build_app()
        client = TestClient(app)
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        return app, client, f"/api/projects/{project}/chats/{chat}"

    def _wait_history(self, client, base, predicate, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            history = client.get(f"{base}/history").json()
            if predicate(history):
                return history
        raise AssertionError("history never satisfied the predicate")

    def test_message_endpoint_returns_immediately(self, tmp_path, monkeypatch):
        _, client, base = self._workbench(tmp_path, monkeypatch)
        response = client.post(f"{base}/messages", json={"message": "quick"})
        started = response.json()
        assert started["started"] is True
        assert re.fullmatch(r"op_[0-9a-f]{32}", started["operation_id"])
        # the turn runs and records with no stream attached
        history = self._wait_history(
            client,
            base,
            lambda h: not h["turn_active"] and len(h["messages"]) == 2,
        )
        assert [m["role"] for m in history["messages"]] == ["user", "assistant"]

        # interrupt with no active turn
        idle = client.post(f"{base}/interrupt").json()
        assert idle == {
            "interrupted": False,
            "reviews_cancelled": 0,
            "writers_paused": 0,
        }

        # Unknown/stale approval ids must not look acknowledged to the UI.
        answer = client.post(
            f"{base}/approvals",
            json={"request_id": "apr_x", "decision": "decline"},
        )
        assert answer.status_code == 409
        assert "expired" in answer.json()["detail"]

    def test_pending_approval_survives_history_reconnect(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REASONING_APPROVALS", "interactive")
        _, client, base = self._workbench(tmp_path, monkeypatch)
        response = client.post(f"{base}/messages", json={"message": "approve"})
        started = response.json()
        assert started["started"] is True
        assert re.fullmatch(r"op_[0-9a-f]{32}", started["operation_id"])

        history = self._wait_history(
            client,
            base,
            lambda h: len(h["pending_approvals"]) == 1,
        )
        approval = history["pending_approvals"][0]
        assert approval["kind"] == "command"
        assert approval["command"] == "rm -rf /"
        assert approval["cwd"] == "/tmp"
        # A second snapshot models navigation or a browser reconnect.
        assert client.get(f"{base}/history").json()["pending_approvals"] == [approval]
        listing = client.get("/api/projects").json()["projects"]
        assert listing[0]["chats"][0]["approval_pending"] is True

        answer = client.post(
            f"{base}/approvals",
            json={"request_id": approval["request_id"], "decision": "accept"},
        )
        assert answer.status_code == 200
        assert answer.json() == {"resolved": True}
        completed = self._wait_history(client, base, lambda h: not h["turn_active"])
        assert completed["pending_approvals"] == []
        listing = client.get("/api/projects").json()["projects"]
        assert listing[0]["chats"][0]["approval_pending"] is False

    def test_event_stream_replays_a_turn_to_any_window(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setattr("reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE))
        app = build_app()
        types = []
        seqs = []
        with _live_server(app) as port:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            try:

                def request(method, path, body=None):
                    conn.request(
                        method,
                        path,
                        body=json.dumps(body) if body is not None else None,
                        headers={"Content-Type": "application/json"},
                    )
                    response = conn.getresponse()
                    assert response.status == 200
                    return json.loads(response.read())

                project = request("POST", "/api/projects", {"name": "p"})["id"]
                chat = request("POST", f"/api/projects/{project}/chats", {})["id"]
                base = f"/api/projects/{project}/chats/{chat}"
                assert request("POST", f"{base}/messages", {"message": "quick"})["started"]
                deadline = time.monotonic() + 10
                while request("GET", f"{base}/history")["turn_active"]:
                    assert time.monotonic() < deadline, "turn did not complete"
                    time.sleep(0.02)
                # A window connecting later replays the turn from its cursor.
                conn.request("GET", f"{base}/events?after=0")
                response = conn.getresponse()
                assert response.getheader("content-type").startswith("text/event-stream")
                while True:
                    raw = response.readline()
                    assert raw != b"", f"stream ended early; saw {types}"
                    line = raw.decode().strip()
                    if line.startswith("id: "):
                        seqs.append(int(line[len("id: ") :]))
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[len("data: ") :])
                    types.append(event["type"])
                    if event["type"] == "turn.completed":
                        break
            finally:
                conn.close()
        assert types[0] == "user_message"
        assert "item.completed" in types
        assert seqs == sorted(seqs) and len(seqs) == len(types)
        assert app.state.workbench_maintenance.stop.is_set()
        assert all(not thread.is_alive() for thread in app.state.workbench_maintenance.threads)

    def test_message_during_turn_steers(self, tmp_path, monkeypatch):
        _, client, base = self._workbench(tmp_path, monkeypatch)
        started = client.post(f"{base}/messages", json={"message": "wait"}).json()
        assert started["started"] is True
        operation_id = started["operation_id"]
        assert re.fullmatch(r"op_[0-9a-f]{32}", operation_id)
        deadline = time.time() + 10
        steered = None
        while time.time() < deadline:
            response = client.post(f"{base}/messages", json={"message": "go left"})
            if response.status_code == 200 and response.json().get("steered"):
                steered = response.json()
                break
            time.sleep(0.1)
        assert steered == {"steered": True, "operation_id": operation_id}
        history = self._wait_history(client, base, lambda h: not h["turn_active"])
        assert [m["role"] for m in history["messages"]] == ["user", "user", "assistant"]
        steer_entry = dict(history["messages"][1])
        assert steer_entry.pop("at", None)  # entries are timestamped
        assert steer_entry.pop("operation_id") == operation_id
        assert steer_entry == {
            "role": "user",
            "text": "go left",
            "steered": True,
            "executor_id": "codex",
            "model": "gpt-6-astra",
            "model_provider": "openai",
        }
        assert history["messages"][2]["text"] == "steered:go left"


class TestCompactionEpoch:
    def test_context_compaction_bumps_the_epoch_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setattr(
            "reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE)
        )
        client = TestClient(build_app())
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        base = f"/api/projects/{project}/chats/{chat}"
        epoch = (
            tmp_path
            / "workbench"
            / "projects"
            / project
            / "chats"
            / chat
            / "agent-tasks"
            / "compaction-epoch"
        )
        assert client.post(f"{base}/messages", json={"message": "compact"}).status_code == 200
        deadline = time.time() + 10
        while time.time() < deadline and not epoch.exists():
            time.sleep(0.1)
        assert epoch.exists(), "compaction never bumped the epoch"
        first = epoch.read_text()
        assert first.strip()
        # a second compaction bumps it again
        deadline = time.time() + 10
        while (
            time.time() < deadline
            and client.get(f"{base}/history").json()["turn_active"]
        ):
            time.sleep(0.1)
        assert client.post(f"{base}/messages", json={"message": "compact"}).status_code == 200
        deadline = time.time() + 10
        while time.time() < deadline and epoch.read_text() == first:
            time.sleep(0.1)
        assert epoch.read_text() != first


class TestGoalContinuation:
    def test_unmarked_goal_wakes_a_follow_up_turn(self, tmp_path):
        from reasoning_ledger.tasks import GoalStore

        session = _session(tmp_path)
        goals = GoalStore(session.goal_path)
        goals.set("Prove the bound.")
        _, subscriber = session.subscribe()
        assert session.start_turn("work on it") == "started"
        # first turn ends; the harness continuation turn follows on its own
        first = _collect(subscriber, until="turn.completed")
        # the server flags the end as not-really-final: the goal is
        # unresolved, so the wake is coming
        done = [e for e in first if e["type"] == "turn.completed"]
        assert done and done[0]["will_continue"] is True
        events = _collect(subscriber, until="turn.started", timeout=10)
        assert any(e["type"] == "turn.started" for e in events)
        # mark the goal so the loop stops after this wake
        goals.mark("completed")
        last = _collect(subscriber, until="turn.completed")
        done = [e for e in last if e["type"] == "turn.completed"]
        assert done and done[0]["will_continue"] is False
        _wait_turn_idle(session)
        # no further wake: the goal is marked
        time.sleep(1.0)
        assert not session.turn_active
        # the wake note was never recorded as a user message
        assert [e["role"] for e in session.transcript()].count("user") == 1
        session.close()

    def test_set_refuses_while_a_goal_exists(self, tmp_path):
        from reasoning_ledger.tasks import GoalError, GoalStore

        goals = GoalStore(tmp_path / "goal.json")
        goals.set("Prove the bound.")
        with pytest.raises(GoalError, match="already set"):
            goals.set("Refute the claim.")
        # marked but not yet resolved still counts as extant
        goals.mark("completed")
        with pytest.raises(GoalError, match="already set"):
            goals.set("Refute the claim.")
        goals.clear()
        assert goals.set("Refute the claim.")["text"] == "Refute the claim."

    def test_user_turn_resolves_marks(self, tmp_path):
        from reasoning_ledger.tasks import GoalStore

        session = _session(tmp_path)
        goals = GoalStore(session.goal_path)
        goals.set("Prove the bound.")
        goals.mark("completed")
        _run(session, "next message")  # user turn: completed goal clears
        assert session.goal_state() is None
        goals.set("Refute the claim.")
        goals.mark("blocked", "need the scanned book")
        _run(session, "here you go")
        goal = session.goal_state()
        assert goal["status"] == "active" and goal["reason"] is None
        session.close()

    def test_pending_review_defers_the_wake(self, tmp_path):
        from reasoning_ledger import service
        from reasoning_ledger.review import request_review
        from reasoning_ledger.tasks import GoalStore

        session = _session(tmp_path)
        GoalStore(session.goal_path).set("Prove the bound.")
        store = LedgerStore(session.ledger_root)
        ref = service.reference_payload(store, author="A", title="T")
        claim = service.claim_payload(
            store,
            "Cited.",
            reference_id=ref["reference_id"],
            author={"operator": "op", "agent": "a", "session": "proj/chat-1"},
        )
        request_review(
            store,
            claim_id=claim["claim_id"],
            requested_by={"operator": "op", "agent": "a", "session": "proj/chat-1"},
        )
        events = _run(session, "work on it")
        # the completion is flagged as continuing (reviewers still out),
        # so the UI keeps its mid-turn scroll behavior for the final
        # message
        done = [e for e in events if e["type"] == "turn.completed"]
        assert done and done[0]["will_continue"] is True
        time.sleep(1.5)
        # goal is active and unmarked, but the pending review owns the wake
        assert not session.turn_active
        session.close()

    def test_interrupt_and_failure_caps_stop_the_loop(self, tmp_path):
        from reasoning_ledger.tasks import GoalStore

        session = _session(tmp_path)
        GoalStore(session.goal_path).set("Prove the bound.")
        session._GOAL_WAKE_RETRY_DELAY = 0.01
        calls = []
        session.start_turn = lambda *a, **k: calls.append(a) or "started"

        # a user interrupt suppresses the wake entirely
        session._user_interrupted = True
        session._maybe_continue_goal("completed")
        time.sleep(0.3)
        assert calls == []

        # failed turns retry twice, then stop until a user message
        for _ in range(3):
            session._maybe_continue_goal("failed")
        time.sleep(0.5)
        assert len(calls) == 2
        session.close()


class TestWriterAnnounce:
    def test_stop_pauses_the_chats_writers(self, tmp_path, monkeypatch):
        from reasoning_ledger import service
        from reasoning_ledger.writer import load_writer, request_writer

        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setattr(
            "reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE)
        )
        client = TestClient(build_app())
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        store = LedgerStore(tmp_path / "workbench" / "projects" / project / "ledger")
        author = {"operator": "op", "agent": "a", "session": f"{project}/{chat}"}
        claim = service.claim_payload(store, "Main.", author=author)
        mine = request_writer(
            store,
            brief="A note.",
            filename="artifacts/note.tex",
            slugs=[claim["claim_id"]],
            requested_by=author,
        )
        other = request_writer(
            store,
            brief="Someone else's note.",
            filename="artifacts/other.tex",
            slugs=[claim["claim_id"]],
            requested_by={**author, "session": f"{project}/other-chat"},
        )
        result = client.post(
            f"/api/projects/{project}/chats/{chat}/interrupt"
        ).json()
        assert result["writers_paused"] == 1
        assert load_writer(store.root, mine["writer_id"])["status"] == "paused"
        assert load_writer(store.root, other["writer_id"])["status"] == "pending"
        # paused, not dead: the writer can be continued
        from reasoning_ledger.writer import continue_writer

        continued = continue_writer(
            store.root, mine["writer_id"], brief="Pick the note back up."
        )
        assert continued["status"] == "pending"

    def test_announcer_spawns_and_lands_the_writer_return(
        self, tmp_path, monkeypatch
    ):
        from reasoning_ledger import service
        from reasoning_ledger.writer import load_writer, request_writer

        fake = tmp_path / "fake-writer"
        fake.write_text("#!/bin/sh\ncat > /dev/null\necho 'draft ready'\n")
        fake.chmod(0o755)
        monkeypatch.setenv("REASONING_WRITER_COMMAND", str(fake))
        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setattr(
            "reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE)
        )
        client = TestClient(build_app())
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        store = LedgerStore(tmp_path / "workbench" / "projects" / project / "ledger")
        author = {"operator": "op", "agent": "a", "session": f"{project}/{chat}"}
        claim = service.claim_payload(store, "Main.", author=author)
        state = request_writer(
            store,
            brief="A note.",
            filename="artifacts/note.tex",
            slugs=[claim["claim_id"]],
            requested_by=author,
        )
        # the announcer (3s cadence) spawns the pending runner, then
        # announces the paused return into the requesting chat
        deadline = time.time() + 20
        entries = []
        while time.time() < deadline:
            history = client.get(
                f"/api/projects/{project}/chats/{chat}/history"
            ).json()
            entries = [m for m in history["messages"] if m["role"] == "writer"]
            if entries:
                break
            time.sleep(0.5)
        assert entries, "writer never announced"
        assert entries[0]["writer_id"] == state["writer_id"]
        assert entries[0]["filename"] == "artifacts/note.tex"
        assert entries[0]["status"] == "paused"
        assert entries[0]["text"] == "draft ready"
        final = load_writer(store.root, state["writer_id"])
        assert final["status"] == "paused" and final["announced"] is True

        # a paused writer stays in the session poll (the dock keeps its
        # row) and the detail endpoint serves the full state
        polled = client.get(
            f"/api/projects/{project}/reviews?for_session={project}/{chat}"
        ).json()
        assert [w["writer_id"] for w in polled["writers"]] == [state["writer_id"]]
        assert "report" not in polled["writers"][0]  # slim
        detail = client.get(
            f"/api/projects/{project}/writers/{state['writer_id']}"
        ).json()
        assert detail["report"] == "draft ready"


class TestStopCancelsReviewers:
    def test_interrupt_endpoint_cancels_session_reviews(self, tmp_path, monkeypatch):
        from reasoning_ledger.review import (
            load_review,
            request_writeup_review,
            update_review,
        )

        monkeypatch.setenv("REASONING_WORKBENCH_ROOT", str(tmp_path / "workbench"))
        monkeypatch.setenv("REASONING_REVIEW_COMMAND", "/bin/false")
        monkeypatch.setattr("reasoning_ledger.webapp.ChatSession._command", lambda self: list(FAKE))
        client = TestClient(build_app())
        project = client.post("/api/projects", json={"name": "p"}).json()["id"]
        chat = client.post(f"/api/projects/{project}/chats", json={}).json()["id"]
        store = LedgerStore(tmp_path / "workbench" / "projects" / project / "ledger")
        mine = request_writeup_review(
            store,
            document="D.",
            document_name="artifacts/d.md",
            requested_by={"session": f"{project}/{chat}"},
        )
        other = request_writeup_review(
            store,
            document="D.",
            document_name="artifacts/d.md",
            requested_by={"session": f"{project}/elsewhere"},
        )
        out = client.post(f"/api/projects/{project}/chats/{chat}/interrupt").json()
        assert out["reviews_cancelled"] == 1
        assert load_review(store.root, mine["review_id"])["status"] == "cancelled"
        # another chat's review is untouched
        assert load_review(store.root, other["review_id"])["status"] == "pending"
        update_review(
            store.root,
            other["review_id"],
            status="cancelled",
            announced=True,
            live=None,
        )


class TestErrorPersistence:
    def test_provider_turn_failures_survive_in_the_transcript(self, tmp_path):
        session = _session(tmp_path)
        events = _run(session, "turnfail")

        errors = [event for event in events if event["type"] == "error"]
        assert [event["message"] for event in errors] == ["provider rejected the turn"]
        assert events[-1]["status"] == "failed"
        transcript_errors = [entry["text"] for entry in session.transcript() if entry["role"] == "error"]
        assert transcript_errors == ["provider rejected the turn"]
        session.close()

    def test_turn_errors_survive_in_the_transcript(self, tmp_path):
        # a session whose codex command explodes: the error is published
        # AND recorded, so a reload still shows it
        session = ChatSession(
            tmp_path / "scratch2",
            tmp_path / "ledger",
            "gpt-5.6-sol",
            "workspace-write",
            state_dir=tmp_path,
            codex_command=["/bin/false"],
        )
        (tmp_path / "scratch2").mkdir(exist_ok=True)
        assert session.start_turn("hello") == "started"
        _wait_turn_idle(session, timeout=30)
        roles = [e["role"] for e in session.transcript()]
        assert "error" in roles
        session.close()
