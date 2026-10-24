"""Bug reports and ideas that members send to their organization's administrators."""

SCHEMA = (
    """CREATE TABLE feedback_reports (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
        user_id TEXT NOT NULL REFERENCES users(id), kind TEXT NOT NULL CHECK(kind IN ('bug','idea')),
        body TEXT NOT NULL, context_json TEXT NOT NULL DEFAULT '{}', client_id TEXT NOT NULL,
        created_at INTEGER NOT NULL, resolved_at INTEGER, resolved_by TEXT REFERENCES users(id),
        UNIQUE(organization_id,user_id,client_id)
    )""",
    "CREATE INDEX feedback_report_page ON feedback_reports(organization_id,created_at)",
)
