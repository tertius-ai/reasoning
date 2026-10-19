"""Who may invoke a bot: an owner-controlled server setting checked on every admission and leased operation.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

``owner``: only the bot's owner (the setting of a bot that has none stored).
``members``: the owner and an explicit list of current organization members.
``topic_writers``: everyone who can currently write in a topic the bot is granted.
Every policy still requires the requester's own current write access to the topic.
A runner's ``--allow-other-users`` advertisement does not change this setting.
"""

from __future__ import annotations

import time

from .bot_store import IDENTITY, BotStore
from .store import Store, StoreError

POLICIES = ("owner", "members", "topic_writers")
MAX_MEMBERS = 64
# A requester the setting excludes; recorded as a visible, terminal "Not run" bot activity.
DECLINED = "requester_not_permitted"


def setting(db, bot_id):
    row = db.execute("SELECT policy FROM bot_sharing WHERE bot_id=?", (bot_id,)).fetchone()
    policy = row[0] if row else "owner"
    members = [item[0] for item in db.execute("SELECT user_id FROM bot_invokers WHERE bot_id=? ORDER BY user_id", (bot_id,))]
    return {"policy": policy, "members": members if policy == "members" else []}


def permits(db, bot, user):
    """Whether the owner's setting admits this requester; topic write access is checked separately by every caller."""
    if user == bot["created_by"]:
        return True
    row = db.execute("SELECT policy FROM bot_sharing WHERE bot_id=?", (bot["id"],)).fetchone()
    policy = row[0] if row else "owner"
    if policy == "topic_writers":
        return True
    return policy == "members" and db.execute(
        "SELECT 1 FROM bot_invokers WHERE organization_id=? AND bot_id=? AND user_id=?",
        (bot["organization_id"], bot["id"], user)).fetchone() is not None


def require(db, bot, user):
    if not permits(db, bot, user):
        raise StoreError(403, "the bot owner's 'Who can use this bot' setting does not include this requester")


def withdraw(db, bot, actor):
    """Stop active work and consent for requesters the current setting no longer admits."""
    now, stopped = int(time.time()), []
    for run in db.execute("SELECT * FROM bot_runs WHERE bot_id=? AND status IN ('queued','running','waiting_approval') "
                          "AND requested_by<>?", (bot["id"], bot["created_by"])).fetchall():
        if permits(db, bot, run["requested_by"]):
            continue
        status = "cancelled" if run["status"] == "queued" or run["error_code"] == "approval_transport_ended" else "outcome_unknown"
        db.execute("UPDATE bot_runs SET status=?,error_code=?,lease_hash=NULL,updated_at=? WHERE id=?",
                   (status, DECLINED, now, run["id"]))
        db.execute("UPDATE bot_execution_approvals SET status='denied',decided_at=? "
                   "WHERE run_id=? AND status IN ('pending','approved')", (now, run["id"]))
        db.execute("UPDATE bot_issue_approvals SET status='denied',decided_at=? "
                   "WHERE run_id=? AND status IN ('pending','approved')", (now, run["id"]))
        db.execute("UPDATE bot_compute_approvals SET status='denied' WHERE run_id=? AND status='pending'", (run["id"],))
        Store._audit(db, bot["organization_id"], actor, "bot.inference.withdraw", run["id"])
        stopped.append(run["id"])
    return stopped


class BotSharing:
    def __init__(self, bots: BotStore):
        self.bots, self.store = bots, bots.store

    def configure(self, user, org, bot_id, policy, members=None, *, personal=True):
        members = [] if members is None else members
        if policy not in POLICIES:
            raise StoreError(400, "choose only me, specific members, or everyone who can write in the bot's topics")
        if (not isinstance(members, list) or len(members) > MAX_MEMBERS or len(set(map(str, members))) != len(members)
                or any(not isinstance(person, str) or not IDENTITY.fullmatch(person) for person in members)):
            raise StoreError(400, f"choose at most {MAX_MEMBERS} distinct organization members")
        if (policy == "members") != bool(members):
            raise StoreError(400, "list members only for the specific-members setting, and at least one")
        with self.store.transaction(write=True) as db:
            bot = BotStore._manage(db, user, org, bot_id, personal)
            if not personal and bot["delegation_kind"] == "personal":
                raise StoreError(403, "the owner of a personal bot chooses who can use it in My bots")
            if bot["revoked_at"] is not None:
                raise StoreError(409, "revoked bot identities cannot be reactivated")
            for person in members:
                if person == bot["created_by"]:
                    raise StoreError(400, "the bot owner can always use the bot; choose other members")
                try:
                    Store._require(db, person, org, write=True)
                except StoreError:
                    raise StoreError(400, "choose current organization members who can write") from None
            db.execute("INSERT INTO bot_sharing(bot_id,policy,updated_by,updated_at) VALUES(?,?,?,?) "
                       "ON CONFLICT(bot_id) DO UPDATE SET policy=excluded.policy,updated_by=excluded.updated_by,"
                       "updated_at=excluded.updated_at", (bot_id, policy, user, int(time.time())))
            db.execute("DELETE FROM bot_invokers WHERE bot_id=?", (bot_id,))
            db.executemany("INSERT INTO bot_invokers(organization_id,bot_id,user_id) VALUES(?,?,?)",
                           [(org, bot_id, person) for person in sorted(members)])
            self.store._audit(db, org, user, "bot.sharing", bot_id)
            withdraw(db, bot, user)
            return setting(db, bot_id)
