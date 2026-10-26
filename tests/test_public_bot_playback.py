"""Scoped bot stop fences external work without fabricating a known outcome."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_public_bot_runtime import setup
from test_public_bots import fixture as bot_fixture

from reasoning_ledger.server.bot_runtime import BotRuntime
from reasoning_ledger.server.bot_sharing import BotSharing
from reasoning_ledger.server.store import StoreError


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


def test_queued_stop_is_idempotent_and_never_claimed(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    stopped = runtime.stop(owner["user_id"], owner["organization_id"], run["id"])
    assert stopped["status"] == "cancelled" and stopped["error_code"] == "stop_requested"
    assert runtime.stop(owner["user_id"], owner["organization_id"], run["id"]) == stopped
    assert runtime.claim(bot["token"]) is None
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM audit_events WHERE action='bot.inference.stop'").fetchone()[0] == 1
    assert runtime.listing(owner["user_id"], owner["organization_id"], topics[1])[0]["owner_user_id"] == owner["user_id"]


@pytest.mark.parametrize("approval_state", [None, "pending", "approved", "consumed"])
def test_claimed_stop_fences_lease_and_preserves_unknown_side_effects(fixture, approval_state):
    store, owner, _, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture, local_tools=True)
    claim = runtime.claim(bot["token"])
    approval = None
    if approval_state:
        approval = runtime.request_execution(bot["token"], run["id"], claim["lease"], ["python3", "-c", "print(2)"])
        if approval_state in {"approved", "consumed"}:
            runtime.approve(owner["user_id"], owner["organization_id"], approval["id"], True)
        if approval_state == "consumed":
            runtime.execution(bot["token"], run["id"], claim["lease"], approval["id"], consume=True)
    stopped = runtime.stop(owner["user_id"], owner["organization_id"], run["id"])
    assert stopped["status"] == "outcome_unknown" and stopped["error_code"] == "stop_requested"
    with pytest.raises(StoreError, match="lease is no longer active"):
        runtime.progress(bot["token"], run["id"], claim["lease"], "thinking")
    # The runner's receipt confirms its CLI ended; only a consumed approval keeps the outcome unknown.
    settled = "outcome_unknown" if approval_state == "consumed" else "cancelled"
    receipt = (bot["token"], run["id"], claim["lease"], "succeeded", "No stale completion")
    assert runtime.finish(*receipt)["status"] == settled
    assert runtime.finish(*receipt)["status"] == settled
    with pytest.raises(StoreError, match="lease is no longer active"):
        runtime.finish(bot["token"], run["id"], "another lease", "succeeded", "No stale completion")
    assert all("No stale completion" not in message["content"]
               for message in store.messages(owner["user_id"], owner["organization_id"], run["topic_id"]))
    with store.transaction() as db:
        if approval:
            expected = "consumed" if approval_state == "consumed" else "denied"
            assert db.execute("SELECT status FROM bot_execution_approvals WHERE id=?", (approval["id"],)).fetchone()[0] == expected
    assert runtime.claim(bot["token"]) is None, "a stopped unknown invocation is never automatically requeued"


def test_stop_requires_current_scope_and_requester_owner_or_administrator(fixture):
    store, owner, topics, other_org, _, bots = fixture
    org = owner["organization_id"]

    def person(email, role="member"):
        invitation = store.invite(owner["user_id"], org, email, role, 3600)
        return store.accept_invitation(invitation["token"], user_id=None, name=email.split("@")[0],
                                       password="fixture password long")["user_id"]

    bot_owner, requester, stranger, viewer = person("bot-owner@example.org"), person("requester@example.org"), \
        person("stranger@example.org"), person("viewer@example.org", "viewer")
    bot = bots.create(bot_owner, org, "Personal", [topics[1]], ["messages:read", "messages:write"], personal=True)
    runtime = BotRuntime(bots)
    runtime.advertise(bot["token"], "codex", ["default"], False, False)
    BotSharing(bots).configure(bot_owner, org, bot["id"], "topic_writers")
    model = f"bot/{bot['id']}/default"
    for actor in (requester, bot_owner, owner["user_id"]):
        run = runtime.enqueue(requester, org, topics[1], model, "stop-" + actor, prompt="An explicit fixture request")
        for denied in (stranger, viewer):
            with pytest.raises(StoreError) as error:
                runtime.stop(denied, org, run["id"])
            assert error.value.status == 403
        with pytest.raises(StoreError) as error:
            runtime.stop(actor, other_org, run["id"])
        assert error.value.status == 404
        assert runtime.stop(actor, org, run["id"])["status"] == "cancelled"


def test_stopping_completed_bot_does_not_erase_retained_answer(fixture):
    _, owner, _, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    claim = runtime.claim(bot["token"])
    answer = runtime.finish(bot["token"], run["id"], claim["lease"], "succeeded", "Retained answer")
    stopped = runtime.stop(owner["user_id"], owner["organization_id"], run["id"])
    assert stopped["status"] == answer["status"] == "succeeded" and stopped["output_text"] == answer["output_text"]


def test_concurrent_claim_and_stop_never_claims_a_known_cancelled_invocation(fixture):
    _, owner, _, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    barrier = Barrier(2)

    def stop():
        barrier.wait()
        return runtime.stop(owner["user_id"], owner["organization_id"], run["id"])

    def claim():
        barrier.wait()
        return runtime.claim(bot["token"])

    with ThreadPoolExecutor(max_workers=2) as threads:
        stopped, claimed = threads.submit(stop), threads.submit(claim)
        stopped, claimed = stopped.result(), claimed.result()
    assert stopped["status"] == ("outcome_unknown" if claimed else "cancelled")
    final = runtime.get(owner["user_id"], owner["organization_id"], run["id"])
    assert final["status"] == stopped["status"] and runtime.claim(bot["token"]) is None


def test_concurrent_finish_and_stop_never_posts_after_a_stopped_lease(fixture):
    store, owner, topics, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture)
    claimed = runtime.claim(bot["token"])
    barrier = Barrier(2)

    def stop():
        barrier.wait()
        return runtime.stop(owner["user_id"], owner["organization_id"], run["id"])

    def finish():
        barrier.wait()
        return runtime.finish(bot["token"], run["id"], claimed["lease"], "succeeded", "Concurrent retained result")

    with ThreadPoolExecutor(max_workers=2) as threads:
        stopped, finished = threads.submit(stop), threads.submit(finish)
        stopped, finished = stopped.result(), finished.result()
    posted = [message for message in store.messages(owner["user_id"], owner["organization_id"], topics[1])
              if message["content"] == "Concurrent retained result"]
    # A completion after the stop is the runner's confirmation that its CLI ended, never a reply.
    delivered = finished["status"] == "succeeded"
    assert finished["status"] == ("succeeded" if delivered else "cancelled")
    assert stopped["status"] == ("succeeded" if delivered else "outcome_unknown")
    assert len(posted) == (1 if delivered else 0)


def test_concurrent_approval_and_stop_cannot_reenable_unconsumed_execution(fixture):
    store, owner, _, _, _, _ = fixture
    runtime, bot, _, run, _ = setup(fixture, local_tools=True)
    claimed = runtime.claim(bot["token"])
    approval = runtime.request_execution(bot["token"], run["id"], claimed["lease"], ["python3", "-c", "print(2)"])
    barrier = Barrier(2)

    def stop():
        barrier.wait()
        return runtime.stop(owner["user_id"], owner["organization_id"], run["id"])

    def approve():
        barrier.wait()
        try:
            return runtime.approve(owner["user_id"], owner["organization_id"], approval["id"], True)
        except StoreError as error:
            assert error.status == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as threads:
        stopped, approved = threads.submit(stop), threads.submit(approve)
        assert stopped.result()["status"] == "outcome_unknown"
        approved.result()
    with store.transaction() as db:
        assert db.execute("SELECT status FROM bot_execution_approvals WHERE id=?", (approval["id"],)).fetchone()[0] == "denied"
    with pytest.raises(StoreError):
        runtime.execution(bot["token"], run["id"], claimed["lease"], approval["id"], consume=True)
