"""Versioned question-centered working accounts, separate from mathematical authority."""

SCHEMA = (
    """CREATE TABLE research_accounts (
        organization_id TEXT NOT NULL, topic_id TEXT NOT NULL, head_revision INTEGER NOT NULL,
        review_cycle INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(organization_id,topic_id),
        FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
    )""",
    """CREATE TABLE research_account_versions (
        organization_id TEXT NOT NULL, topic_id TEXT NOT NULL, revision INTEGER NOT NULL,
        body_json TEXT NOT NULL, sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL,
        review_cycle INTEGER NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
        run_id TEXT REFERENCES llm_runs(id), client_id TEXT NOT NULL, arguments_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY(organization_id,topic_id,revision), UNIQUE(organization_id,created_by,client_id),
        FOREIGN KEY(organization_id,topic_id) REFERENCES research_accounts(organization_id,topic_id)
    )""",
    """CREATE TRIGGER research_account_version_update BEFORE UPDATE ON research_account_versions
        BEGIN SELECT RAISE(ABORT,'working account versions are immutable'); END""",
    """CREATE TRIGGER research_account_version_delete BEFORE DELETE ON research_account_versions
        BEGIN SELECT RAISE(ABORT,'working account versions are retained'); END""",
)
