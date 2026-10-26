"""Single-use account recovery and encrypted security notification delivery."""

SCHEMA = (
    """CREATE TABLE account_mail (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), email TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('reset','password_changed')),
        client_id TEXT NOT NULL, token_hash TEXT UNIQUE, password_binding TEXT,
        organization_id TEXT REFERENCES organizations(id), requested_by TEXT REFERENCES users(id),
        configuration_hash TEXT NOT NULL, ciphertext BLOB,
        state TEXT NOT NULL CHECK(state IN ('queued','sending','sent','rejected','unknown','cancelled')),
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, consumed_at INTEGER,
        provider_id TEXT, error_code TEXT,
        UNIQUE(user_id,kind,client_id)
    )""",
    "CREATE INDEX account_mail_queue ON account_mail(state,created_at)",
    "CREATE INDEX account_mail_user ON account_mail(user_id,created_at)",
)
