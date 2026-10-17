"""Millisecond bot wait stages: server stamps, runner reports, and old peers.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import json
import sys
import time

import pytest
from pydantic import ValidationError
from test_public_bots import fixture as bot_fixture

from reasoning_ledger.deployment.private import read_document
from reasoning_ledger.server.bot_activity import cli_stage
from reasoning_ledger.server.bot_agent import invoke
from reasoning_ledger.server.bot_api import Completion
from reasoning_ledger.server.bot_client import BotUnavailable
from reasoning_ledger.server.bot_runner import runtime_step
from reasoning_ledger.server.bot_runtime import RUNNER_STAGES, BotRuntime
from reasoning_ledger.server.store import StoreError

LEASE = "a" * 43
UNKNOWN_RUNNER = dict.fromkeys(RUNNER_STAGES)


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


def setup(fixture):
    store, owner, topics, _, _, bots = fixture
    bot = bots.create(owner["user_id"], owner["organization_id"], "Codex", topics[1:],
                      ["messages:read", "messages:write"], personal=True)
    runtime = BotRuntime(bots)
    advertised = runtime.advertise(bot["token"], "codex", ["default"], False, False)
    message = store.post_message(owner["user_id"], owner["organization_id"], topics[1], "Explain 2=2", "request")
    run = runtime.enqueue(owner["user_id"], owner["organization_id"], topics[1], f"bot/{bot['id']}/default", "timed",
                          through=message["sequence"])
    return runtime, bot, run, advertised


def test_server_stages_are_millisecond_stamps_and_unreported_stages_stay_null(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, run, advertised = setup(fixture)
    assert advertised["run_timing"] is True
    before = int(time.time() * 1000)
    assert run["timing"]["enqueued_at_ms"] is not None and run["timing"]["claimed_at_ms"] is None
    assert run["timing"]["queue_wait_ms"] is None and run["timing"]["runner"] == UNKNOWN_RUNNER
    claimed = runtime.claim(bot["token"])
    assert before - 5000 <= claimed["timing"]["claimed_at_ms"] <= int(time.time() * 1000)
    assert claimed["timing"]["queue_wait_ms"] == claimed["timing"]["claimed_at_ms"] - claimed["timing"]["enqueued_at_ms"]
    report = {"cli_started_ms": 40, "cli_ready_ms": 900, "first_text_ms": 4000, "final_text_ms": 4100,
              "cli_exited_ms": 4300, "resolved_model": "claude-test-1"}
    finished = runtime.finish(bot["token"], run["id"], claimed["lease"], "succeeded", "By reflexivity.", timing=report)
    timing = finished["timing"]
    assert timing["runner"] == {key: report[key] for key in RUNNER_STAGES} and timing["resolved_model"] == "claude-test-1"
    assert timing["claim_to_commit_ms"] == timing["committed_at_ms"] - timing["claimed_at_ms"] >= 0
    # A replayed receipt neither fails on, nor replaces, the retained report.
    again = runtime.finish(bot["token"], run["id"], claimed["lease"], "succeeded", "By reflexivity.", timing={"cli_ready_ms": 1})
    assert again["timing"] == timing
    listed = runtime.listing(owner["user_id"], owner["organization_id"], topics[1])[0]
    assert listed["timing"] == timing
    assert "By reflexivity" not in json.dumps(timing)


def test_failures_have_no_commit_and_runs_from_before_the_columns_stay_unknown(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, run, _ = setup(fixture)
    claimed = runtime.claim(bot["token"])
    failed = runtime.finish(bot["token"], run["id"], claimed["lease"], "failed", error="cli_failed")
    assert failed["timing"]["committed_at_ms"] is None and failed["timing"]["claim_to_commit_ms"] is None
    assert failed["timing"]["runner"] == UNKNOWN_RUNNER and failed["timing"]["resolved_model"] is None
    with store.transaction(write=True) as db:
        db.execute("UPDATE bot_runs SET enqueued_at_ms=NULL,claimed_at_ms=NULL,committed_at_ms=NULL,runner_timing_json=NULL")
    legacy = runtime.listing(owner["user_id"], owner["organization_id"], topics[1])[0]
    # Second-rounded created_at/updated_at are present but never used to fill stages.
    assert legacy["created_at"] and legacy["updated_at"]
    assert legacy["timing"] == {"enqueued_at_ms": None, "claimed_at_ms": None, "committed_at_ms": None,
                                "queue_wait_ms": None, "claim_to_commit_ms": None, "runner": UNKNOWN_RUNNER,
                                "resolved_model": None}


@pytest.mark.parametrize("report", [
    {"cli_ready_ms": -1}, {"cli_ready_ms": 1.5}, {"cli_ready_ms": True}, {"cli_ready_ms": 86_400_001},
    {"prompt": "secret"}, {"resolved_model": "two words"}, {"resolved_model": 3}, "fast",
])
def test_runner_reports_are_bounded_numbers_and_a_model_identifier_only(fixture, report):
    runtime, bot, run, _ = setup(fixture)
    claimed = runtime.claim(bot["token"])
    with pytest.raises(StoreError, match="invalid bot runner timing"):
        runtime.finish(bot["token"], run["id"], claimed["lease"], "succeeded", "Answer", timing=report)
    with pytest.raises(ValidationError):
        Completion.model_validate({"lease": LEASE, "status": "succeeded", "output": "Answer", "timing": report})


def test_wire_completion_accepts_older_runners_without_timing():
    older = Completion.model_validate({"lease": LEASE, "status": "succeeded", "output": "Answer"})
    assert "timing" not in older.model_dump(exclude_unset=True)
    newer = Completion.model_validate({"lease": LEASE, "status": "succeeded", "output": "Answer",
                                       "timing": {"cli_ready_ms": 5, "resolved_model": "default"}})
    assert newer.model_dump(exclude_unset=True)["timing"] == {"cli_ready_ms": 5, "resolved_model": "default"}


def runner_client(runtime, bot, *, server_timing=True, lose_first_finish=False):
    class Client:
        finishes = []
        lose = lose_first_finish

        def messages(self, after, limit):
            return []

        def _request(self, method, path, body=None):
            if path == "/api/bot/runtime":
                advertised = runtime.advertise(bot["token"], **body)
                return advertised if server_timing else {key: advertised[key] for key in ("models", "policy", "mention_feedback")}
            if path == "/api/bot/runs/claim":
                return runtime.claim(bot["token"])
            if path.endswith("/progress"):
                return runtime.progress(bot["token"], path.split("/")[4], **body)
            assert path.endswith("/finish")
            if not server_timing and "timing" in body:
                raise AssertionError("an older server rejects unknown completion fields")
            self.finishes.append(body)
            result = runtime.finish(bot["token"], path.split("/")[4], **body)
            if self.lose:
                self.lose = False
                raise BotUnavailable("completion receipt lost")
            return result

    return Client()


def timed_runner(engine, cwd, prompt, local_tools, session, *, timing, **options):
    started = time.monotonic()
    timing.update(cli_started=started, cli_ready=started + 0.5, first_text=started + 2.0, final_text=started + 2.25,
                  cli_exited=started + 2.5, model="claude-test-1")
    return {"text": "Answer", "session": None}


@pytest.mark.parametrize("server_timing", [True, False])
@pytest.mark.parametrize("lost_receipt", [False, True])
def test_runner_reports_claim_relative_stages_only_to_servers_that_accept_them(fixture, tmp_path, server_timing, lost_receipt):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, run, _ = setup(fixture)
    root, workspace = tmp_path / "private", tmp_path / "workspace"
    root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    client = runner_client(runtime, bot, server_timing=server_timing, lose_first_finish=lost_receipt)
    agent = {"engine": "codex", "workspace": str(workspace), "local_tools": False, "models": ["default"]}
    binding = {"server": "https://example.org", "bot_id": bot["id"], "owner_id": owner["user_id"], "allow_other_users": False}
    configuration = {"server": binding["server"], "token": bot["token"]}
    if lost_receipt:
        with pytest.raises(BotUnavailable):
            runtime_step(client, root, configuration, agent, binding, runner=timed_runner)
        assert read_document(root / "runtime-runner.json")["pending"]["timing"]["cli_ready_ms"] is not None
    runtime_step(client, root, configuration, agent, binding, runner=timed_runner)
    timing = runtime.listing(owner["user_id"], owner["organization_id"], topics[1])[0]["timing"]
    if not server_timing:
        assert all("timing" not in body for body in client.finishes)
        assert timing["runner"] == UNKNOWN_RUNNER and timing["claimed_at_ms"] is not None
        return
    sent = [body["timing"] for body in client.finishes]
    assert len(sent) == (2 if lost_receipt else 1) and all(item == sent[0] for item in sent)
    stages = timing["runner"]
    assert 0 <= stages["cli_started_ms"] <= stages["cli_ready_ms"] <= stages["first_text_ms"]
    assert stages["first_text_ms"] <= stages["final_text_ms"] <= stages["cli_exited_ms"]
    assert stages["cli_ready_ms"] - stages["cli_started_ms"] == 500
    assert timing["resolved_model"] == "claude-test-1" and timing["committed_at_ms"] is not None


def test_runner_without_timing_support_leaves_runner_stages_unknown(fixture, tmp_path):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, run, _ = setup(fixture)
    root, workspace = tmp_path / "private", tmp_path / "workspace"
    root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)

    def runner(engine, cwd, prompt, local_tools, session, **options):
        assert "timing" not in options
        return {"text": "Answer", "session": None}

    agent = {"engine": "codex", "workspace": str(workspace), "local_tools": False, "models": ["default"]}
    binding = {"server": "https://example.org", "bot_id": bot["id"], "owner_id": owner["user_id"], "allow_other_users": False}
    runtime_step(runner_client(runtime, bot), root, {"server": binding["server"], "token": bot["token"]}, agent, binding,
                 runner=runner)
    timing = runtime.listing(owner["user_id"], owner["organization_id"], topics[1])[0]["timing"]
    assert timing["runner"] == UNKNOWN_RUNNER and timing["resolved_model"] is None
    assert timing["queue_wait_ms"] is not None and timing["claim_to_commit_ms"] is not None


def test_cli_stage_markers_never_carry_content():
    assert cli_stage({"type": "thread.started", "thread_id": "secret-thread"}) == ("ready", None)
    assert cli_stage({"type": "system", "subtype": "init", "model": "claude-opus-4"}) == ("ready", "claude-opus-4")
    assert cli_stage({"type": "system", "subtype": "init", "model": "not a model id"}) == ("ready", None)
    assert cli_stage({"type": "item.completed", "item": {"type": "agent_message", "text": "Private answer"}}) == ("text", None)
    assert cli_stage({"type": "item.started", "item": {"type": "agent_message", "text": ""}}) == (None, None)
    assert cli_stage({"type": "assistant", "message": {"content": [{"type": "text", "text": "Private answer"}]}}) == ("text", None)
    assert cli_stage({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "x"}]}}) == (None, None)
    assert cli_stage("text") == (None, None)


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_invoke_records_when_the_runner_observed_each_cli_stage(tmp_path, engine):
    session = "0123456789abcdef0123456789abcdef"
    events = {
        "codex": [{"type": "thread.started", "thread_id": session}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "Answer"}},
                  {"type": "turn.completed", "usage": {}}],
        "claude": [{"type": "system", "subtype": "init", "session_id": session, "model": "claude-test-1"},
                   {"type": "assistant", "message": {"content": [{"type": "text", "text": "Answer"}]}},
                   {"type": "result", "result": "Answer", "session_id": session, "is_error": False}],
    }[engine]
    script = tmp_path / "fake-cli"
    script.write_text(f"#!{sys.executable}\nimport json, sys, time\nsys.stdin.read()\n"
                      f"for event in {events!r}:\n    time.sleep(0.3)\n    print(json.dumps(event), flush=True)\n")
    script.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stamps = {}
    result = invoke(engine, workspace, "Question", executable=script, timing=stamps)
    assert result["text"] == "Answer"
    assert stamps["cli_started"] < stamps["cli_ready"] < stamps["first_text"] <= stamps["final_text"] <= stamps["cli_exited"]
    assert stamps.get("model") == ("claude-test-1" if engine == "claude" else None)
    assert set(stamps) <= {"cli_started", "cli_ready", "first_text", "final_text", "cli_exited", "model"}
