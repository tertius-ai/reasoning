"""Pictures and PDFs shared in a direct conversation, kept apart from topic media so no bot or model path reads them."""

SCHEMA = (
    """CREATE TABLE direct_media (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
        created_by TEXT NOT NULL REFERENCES users(id),
        name TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        complete INTEGER NOT NULL DEFAULT 0 CHECK(complete IN (0,1)), created_at INTEGER NOT NULL,
        FOREIGN KEY(organization_id,conversation_id) REFERENCES direct_conversations(organization_id,id)
    )""",
    """CREATE TABLE direct_media_chunks (
        media_id TEXT NOT NULL REFERENCES direct_media(id) ON DELETE CASCADE,
        chunk_index INTEGER NOT NULL, data_base64 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        PRIMARY KEY(media_id,chunk_index)
    )""",
    "CREATE INDEX direct_media_scope ON direct_media(organization_id,conversation_id,created_at)",
)
