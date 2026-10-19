"""Who other than its owner may invoke a bot: an owner-controlled server setting.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

SCHEMA = (
    # A bot without a row is usable only by its owner.
    """CREATE TABLE bot_sharing (
        bot_id TEXT PRIMARY KEY REFERENCES bots(id),
        policy TEXT NOT NULL CHECK(policy IN ('owner','members','topic_writers')),
        updated_by TEXT NOT NULL REFERENCES users(id), updated_at INTEGER NOT NULL
    )""",
    # The explicit members of the 'members' policy; removing a member from the organization removes the row.
    """CREATE TABLE bot_invokers (
        organization_id TEXT NOT NULL, bot_id TEXT NOT NULL, user_id TEXT NOT NULL,
        PRIMARY KEY(bot_id,user_id),
        FOREIGN KEY(organization_id,bot_id) REFERENCES bots(organization_id,id),
        FOREIGN KEY(organization_id,user_id) REFERENCES memberships(organization_id,user_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX bot_invoker_member ON bot_invokers(organization_id,user_id)",
)
