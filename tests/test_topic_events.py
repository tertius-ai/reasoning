"""Per-topic browser event streams over real HTTP: access, ordering, catch-up, limits and shutdown."""

from __future__ import annotations

import contextlib
import json
import queue
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from test_public_inference import MODELS, enqueue, prepare
from test_public_server import installation as installation
from test_public_server import invited

from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.openrouter import Usage
from reasoning_ledger.server.topic_events import StreamLimits, TopicEvents, _RunPatches, _values


@contextmanager
def live(config, **options):
    """The TestClient buffers whole responses, so streams need a real server."""
    app = create_app(Config(config.data_dir, config.public_url, inference_enabled=False, **options))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield app, f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "server with open streams did not stop"


class Stream:
    """Reads one event stream in a thread over a plain HTTP/1.0 socket, which closes at once on request.

    Events are (name, data) pairs; comments are ('', text)."""

    def __init__(self, base, path, headers):
        self.events, self.status, self.headers = queue.Queue(), None, {}
        self.opened, self.done = threading.Event(), threading.Event()
        host, port = base.removeprefix("http://").split(":")
        self.socket = socket.create_connection((host, int(port)))
        lines = [f"GET {path} HTTP/1.0", "Host: " + host, "Accept: text/event-stream"]
        lines += [f"{name}: {value}" for name, value in headers.items()]
        self.socket.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        assert self.opened.wait(10)

    def _read(self):
        try:
            stream = self.socket.makefile("rb")
            self.status = int(stream.readline().split()[1])
            while (line := stream.readline().decode().rstrip("\r\n")):
                name, _, value = line.partition(":")
                self.headers[name.strip().lower()] = value.strip()
            self.opened.set()
            name, data = None, []
            for raw in stream:
                line = raw.decode().rstrip("\r\n")
                if line.startswith(":"):
                    self.events.put(("", line[1:].strip()))
                elif line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].strip())
                elif line == "" and name:
                    self.events.put((name, json.loads("\n".join(data))))
                    name, data = None, []
        except (OSError, ValueError, IndexError):
            pass
        finally:
            self.opened.set()
            self.done.set()

    def next(self, name=None, timeout=5.0, where=lambda _data: True):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no {name or 'event'} within {timeout} s")
            try:
                event = self.events.get(timeout=remaining)
            except queue.Empty:
                continue
            if (name is None or event[0] == name) and (event[0] == "" or where(event[1])):
                return event

    def close(self):
        with contextlib.suppress(OSError):
            self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()
        self.thread.join(timeout=5)


@pytest.fixture
def topic(installation):
    config, state, client, owner, topics = prepare(installation)
    return config, state, client, owner, topics


def path(owner, topic_id):
    return f"/api/organizations/{owner['organization_id']}/topics/{topic_id}/events"


def bearer(client):
    return {"Authorization": client.headers["Authorization"]}


def test_stream_requires_a_session_and_topic_access(topic, installation):
    config, _, client, owner, topics = topic
    _, member_client, _ = invited(installation)
    with live(config) as (app, base):
        with httpx.Client(base_url=base) as anonymous:
            assert anonymous.get(path(owner, topics[1]["id"])).status_code == 401
            assert anonymous.get(path(owner, topics[1]["id"]), headers={"Authorization": "Bearer nope"}).status_code == 401
            other = "f" * 32
            assert anonymous.get(path(owner, other), headers=bearer(client)).status_code == 404
            # A private project hides its topics from organization members.
            root = f"/api/organizations/{owner['organization_id']}"
            project = client.post(root + "/projects", json={"name": "Private", "visibility": "private",
                                                            "owner_id": owner["user_id"]}).json()
            channel = client.post(root + "/channels", json={"name": "secret", "project_id": project["id"]}).json()
            hidden = client.post(root + "/topics", json={"name": "hidden", "kind": "alpha", "channel_id": channel["id"]}).json()
            assert anonymous.get(path(owner, hidden["id"]), headers=bearer(member_client)).status_code == 404
        assert app.state.topic_events.open_streams == 0


def test_stream_keeps_security_headers_and_sends_initial_state(topic):
    config, _, client, owner, topics = topic
    with live(config) as (_, base):
        stream = Stream(base, path(owner, topics[1]["id"]), bearer(client))
        try:
            headers = stream.headers
            assert headers["content-type"].startswith("text/event-stream")
            assert headers["cache-control"] == "no-store, no-transform"
            assert headers["x-content-type-options"] == "nosniff" and headers["referrer-policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in headers["content-security-policy"]
            assert headers["strict-transport-security"] == "max-age=31536000"
            assert headers["x-accel-buffering"] == "no"
            ready = stream.next("ready")[1]
            assert ready["channels"] == ["agents", "bot-runs", "goal", "messages", "runs"]
            assert stream.next("messages")[1] == {"sequence": 1, "updates": 0}
            assert stream.next("runs")[1] == {"order": [], "runs": []}
            assert stream.next("agents")[1] == [] and stream.next("goal")[1] is None
            assert stream.next("bot-runs")[1] == []
        finally:
            stream.close()
        # Discussion topics carry only messages and bot invocations.
        stream = Stream(base, path(owner, topics[0]["id"]), bearer(client))
        try:
            assert stream.next("ready")[1]["channels"] == ["bot-runs", "messages"]
            assert stream.next("messages")[1] == {"sequence": 0, "updates": 0}
        finally:
            stream.close()


def test_writes_reach_the_stream_in_order_without_duplicates(topic):
    config, state, client, owner, topics = topic
    org, user, beta = owner["organization_id"], owner["user_id"], topics[1]["id"]
    with live(config) as (app, base):
        app.state.topic_events.limits = StreamLimits(idle_interval=3.0, active_interval=0.2)
        stream = Stream(base, path(owner, beta), bearer(client))
        try:
            stream.next("bot-runs")
            started = time.monotonic()
            message = httpx.post(base + f"/api/organizations/{org}/topics/{beta}/messages", headers=bearer(client),
                                 json={"content": "Another step", "client_id": "stream-2"}).json()
            # The write nudges the watcher, so the idle interval does not delay it.
            assert stream.next("messages")[1] == {"sequence": message["sequence"], "updates": 0}
            assert time.monotonic() - started < 1.5
            run = enqueue(state, owner, beta, models=MODELS[:1])[0]
            first = stream.next("runs", where=lambda data: data["runs"])[1]
            assert first["order"] == [run["id"]] and first["runs"][0]["status"] == "queued"
            claimed = state.claim()
            assert state.progress(claimed, "Hel", "gen-1")
            grown = stream.next("runs", where=lambda data: any("Hel" in json.dumps(item) for item in data["runs"]))[1]
            assert grown["runs"][0]["status"] == "running"
            assert state.progress(claimed, "Hello, world", "gen-1")
            appended = stream.next("runs", where=lambda data: any("output_append" in item for item in data["runs"]))[1]
            assert appended["runs"][0]["output_append"] == "lo, world" and "output_text" not in appended["runs"][0]
            assert state.finish(claimed, "succeeded", usage=Usage(10, 2, 3))
            # The final message is announced before the run that links to it.
            names = []
            while "runs" not in names or "messages" not in names:
                event = stream.next()
                names.append(event[0])
                if event[0] == "runs":
                    final = event[1]["runs"][0]
            assert names.index("messages") < names.index("runs")
            assert final["status"] == "succeeded" and final["message_id"]
            assert "output_append" in final and final["output_append"] == ""
            assert state.run(user, org, run["id"])["output_text"] == "Hello, world"
        finally:
            stream.close()


def test_cancellation_is_streamed(topic):
    config, state, client, owner, topics = topic
    org, beta = owner["organization_id"], topics[1]["id"]
    run = enqueue(state, owner, beta, models=MODELS[:1])[0]
    claimed = state.claim()
    with live(config) as (_, base):
        stream = Stream(base, path(owner, beta), bearer(client))
        try:
            assert stream.next("runs")[1]["runs"][0]["status"] == "running"
            assert httpx.post(base + f"/api/organizations/{org}/inference-runs/{run['id']}/cancel", headers=bearer(client),
                              json={}).status_code == 200
            assert stream.next("runs", where=lambda data: data["runs"])[1]["runs"][0]["cancel_requested"] == 1
            assert state.finish(claimed, "succeeded", usage=Usage(1, 1, 1)) is True
            assert stream.next("runs", where=lambda data: data["runs"])[1]["runs"][0]["status"] == "cancelled"
        finally:
            stream.close()


def test_reconnect_catches_up_from_the_client_cursor(topic):
    config, state, client, owner, topics = topic
    org, beta = owner["organization_id"], topics[1]["id"]
    run = enqueue(state, owner, beta, models=MODELS[:1])[0]
    claimed = state.claim()
    state.progress(claimed, "Partial", "gen-1")
    with live(config) as (_, base):
        stream = Stream(base, path(owner, beta), bearer(client))
        cursor = stream.next("messages")[1]["sequence"]
        assert stream.next("runs")[1]["runs"][0]["output_text"] == "Partial"
        stream.close()
        # Missed while disconnected: two messages, an edit, and more output.
        posted = [httpx.post(base + f"/api/organizations/{org}/topics/{beta}/messages", headers=bearer(client),
                             json={"content": f"Offline {index}", "client_id": f"offline-{index}"}).json() for index in range(2)]
        httpx.patch(base + f"/api/organizations/{org}/topics/{beta}/messages/{posted[0]['id']}", headers=bearer(client),
                    json={"content": "Offline 0, edited", "revision": 0}).raise_for_status()
        state.progress(claimed, "Partial output", "gen-1")
        stream = Stream(base, path(owner, beta), bearer(client))
        try:
            watermark = stream.next("messages")[1]
            assert watermark["sequence"] == posted[-1]["sequence"] and watermark["updates"] > 0
            # The client's cursor fetch then returns exactly the missed messages, once each.
            missed = httpx.get(base + f"/api/organizations/{org}/topics/{beta}/messages?after={cursor}", headers=bearer(client)).json()
            assert [item["id"] for item in missed] == [item["id"] for item in posted]
            # A new connection sends the whole output, never an append against another connection's state.
            resumed = stream.next("runs")[1]["runs"][0]
            assert resumed["id"] == run["id"] and resumed["output_text"] == "Partial output" and "output_append" not in resumed
        finally:
            stream.close()


def test_access_and_session_are_rechecked_while_streaming(topic, installation):
    config, _, client, owner, topics = topic
    _, member_client, member = invited(installation)
    org, beta = owner["organization_id"], topics[1]["id"]
    with live(config) as (app, base):
        app.state.topic_events.limits = StreamLimits(recheck=0.2)
        stream = Stream(base, path(owner, beta), bearer(member_client))
        try:
            stream.next("bot-runs")
            client.delete(f"/api/organizations/{org}/members/{member['user_id']}").raise_for_status()
            assert stream.next("end", timeout=5)[1] == {"reason": "access"}
            assert stream.done.wait(5)
        finally:
            stream.close()
        stream = Stream(base, path(owner, beta), bearer(client))
        try:
            stream.next("bot-runs")
            httpx.post(base + "/api/auth/logout", headers=bearer(client), json={}).raise_for_status()
            assert stream.next("end", timeout=5)[1] == {"reason": "session"}
        finally:
            stream.close()
        deadline = time.monotonic() + 5
        while app.state.topic_events.open_streams and time.monotonic() < deadline:
            time.sleep(0.05)
        assert app.state.topic_events.open_streams == 0 and app.state.topic_events.watchers == 0


def test_streams_are_bounded_released_on_disconnect_and_closed_on_shutdown(topic):
    config, _, client, owner, topics = topic
    with live(config) as (app, base):
        hub = app.state.topic_events
        hub.limits = StreamLimits(total=3, per_user=2, heartbeat=0.2)
        streams = [Stream(base, path(owner, topics[1]["id"]), bearer(client)) for _ in range(2)]
        try:
            for stream in streams:
                stream.next("ready")
            refused = httpx.get(base + path(owner, topics[0]["id"]), headers=bearer(client))
            assert refused.status_code == 429 and refused.headers["cache-control"] == "no-store"
            assert hub.open_streams == 2 and hub.watchers == 1
            assert streams[0].next("", timeout=2) == ("", "keepalive")
        finally:
            for stream in streams:
                stream.close()
        deadline = time.monotonic() + 5
        while (hub.open_streams or hub.watchers) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hub.open_streams == 0 and hub.watchers == 0
        hub.limits = StreamLimits(lifetime=0.5)
        stream = Stream(base, path(owner, topics[1]["id"]), bearer(client))
        try:
            assert stream.next("end", timeout=5)[1] == {"reason": "lifetime"}
        finally:
            stream.close()
        stream = Stream(base, path(owner, topics[1]["id"]), bearer(client))
        hub.limits = StreamLimits()
        stream.next("bot-runs")
        hub.close()
        assert stream.next("end", timeout=3)[1] == {"reason": "shutdown"}
        assert stream.done.wait(5)
        stream.close()
        assert httpx.get(base + path(owner, topics[1]["id"]), headers=bearer(client)).status_code == 503


def test_disabled_streams_leave_polling_in_place(topic):
    config, _, client, owner, topics = topic
    with live(config, topic_events=False) as (_, base):
        response = httpx.get(base + path(owner, topics[1]["id"]), headers=bearer(client))
        assert response.status_code == 503 and response.json() == {"detail": "topic event stream disabled"}
        assert httpx.get(base + f"/api/organizations/{owner['organization_id']}/topics/{topics[1]['id']}/messages",
                         headers=bearer(client)).status_code == 200


def test_cookie_sessions_can_stream_without_csrf(tmp_path):
    config = Config(tmp_path / "state", "http://localhost:8766")
    with live(config) as (_, base):
        setup = httpx.post(base + "/api/setup", json={
            "setup_token": (config.data_dir / "setup-token").read_text(), "email": "owner@example.org", "name": "Owner",
            "password": "a very long test password", "organization_name": "Mathematics"})
        assert setup.status_code == 201
        cookies, org = setup.cookies, setup.json()["organization_id"]
        csrf = {"X-CSRF-Token": cookies["reasoning_csrf"], "Origin": "http://localhost:8766"}
        project = httpx.post(base + f"/api/organizations/{org}/projects", json={"name": "P"}, cookies=cookies, headers=csrf).json()
        channel = httpx.post(base + f"/api/organizations/{org}/channels", json={"name": "c", "project_id": project["id"]},
                             cookies=cookies, headers=csrf).json()
        topic = httpx.post(base + f"/api/organizations/{org}/topics", json={"name": "t", "kind": "alpha", "channel_id": channel["id"]},
                           cookies=cookies, headers=csrf).json()
        stream = Stream(base, f"/api/organizations/{org}/topics/{topic['id']}/events",
                        {"Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())})
        try:
            assert stream.status == 200 and stream.next("messages")[1] == {"sequence": 0, "updates": 0}
        finally:
            stream.close()


def test_run_patches_resend_only_changes_and_fall_back_to_full_output():
    patches = _RunPatches()
    first = [{"id": "a", "status": "running", "output_text": "One"}, {"id": "b", "status": "succeeded", "output_text": "Done"}]
    assert patches.patch(first) == {"order": ["a", "b"], "runs": first}
    assert patches.patch(first) is None
    grown = [{"id": "a", "status": "running", "output_text": "One two"}, first[1]]
    assert patches.patch(grown) == {"order": ["a", "b"], "runs": [{"id": "a", "status": "running", "output_append": " two"}]}
    rewritten = [{"id": "a", "status": "running", "output_text": "Other"}, first[1]]
    assert patches.patch(rewritten) == {"order": ["a", "b"], "runs": [rewritten[0]]}
    assert patches.patch([first[1]]) == {"order": ["b"], "runs": []}


def test_signatures_use_positional_values_on_both_databases():
    from reasoning_ledger.server.postgres import Row

    # PostgreSQL rows are mappings: iterating them yields column names, which would hide every change.
    assert _values(Row(["id", "status"], ["run", "running"])) == ("run", "running")


def test_nudges_wake_only_the_written_topic():
    hub = TopicEvents(store=None, inference=None)

    class Watcher:
        def __init__(self):
            self.wake = threading.Event()

    hub._watchers = {("o", "a" * 32): Watcher(), ("o", "b" * 32): Watcher()}
    hub.nudge(f"/api/organizations/o/topics/{'a' * 32}/messages")
    assert hub._watchers[("o", "a" * 32)].wake.is_set() and not hub._watchers[("o", "b" * 32)].wake.is_set()
    hub.nudge("/api/organizations/o/inference-runs/r/cancel")
    assert hub._watchers[("o", "b" * 32)].wake.is_set()


def test_managed_proxy_streams_events_without_buffering():
    from reasoning_ledger.deployment import host

    proxy = host.caddyfile("https://chat.example.org")
    # Caddy's reverse_proxy flushes text/event-stream at once unless a flush interval or buffering is configured, and
    # its encode directive skips responses marked no-transform (asserted on the stream response above).
    for directive in ("flush_interval", "buffer_requests", "buffer_responses", "request_buffers", "response_buffers",
                      "read_timeout", "write_timeout", "timeouts", "header_down"):
        assert directive not in proxy
    assert "reverse_proxy 127.0.0.1:8766" in proxy and "encode zstd gzip" in proxy
