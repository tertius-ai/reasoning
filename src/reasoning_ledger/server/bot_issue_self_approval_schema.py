"""Owner-scoped self-approval of GitHub issue requests and who decided each request.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

SCHEMA = (
    "ALTER TABLE bot_developer_permissions ADD COLUMN issue_self_approvers_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE bot_developer_permissions ADD COLUMN issue_daily_limit INTEGER NOT NULL DEFAULT 3",
    # NULL approver_id means the bot owner decides; request_sha256 identifies the bot's submitted inputs,
    # document_sha256 the exact published document including server-generated attribution.
    "ALTER TABLE bot_issue_approvals ADD COLUMN approver_id TEXT",
    "ALTER TABLE bot_issue_approvals ADD COLUMN decided_by TEXT",
    "ALTER TABLE bot_issue_approvals ADD COLUMN request_sha256 TEXT",
    "CREATE INDEX bot_issue_approver ON bot_issue_approvals(bot_id,approver_id,status)",
)
