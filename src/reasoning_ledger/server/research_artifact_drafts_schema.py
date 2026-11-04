"""Owner-scoped resumable text drafts and their retained source chunks."""

SCHEMA = (
    """CREATE TABLE research_artifact_drafts (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, project_id TEXT NOT NULL, topic_id TEXT NOT NULL,
        created_by TEXT NOT NULL REFERENCES users(id), client_id TEXT NOT NULL, path TEXT NOT NULL,
        expected_sha256 TEXT, run_id TEXT REFERENCES llm_runs(id), total_characters INTEGER NOT NULL DEFAULT 0,
        size_bytes INTEGER NOT NULL DEFAULT 0, chunk_count INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL,
        result_json TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        UNIQUE(organization_id,created_by,client_id),
        FOREIGN KEY(organization_id,project_id) REFERENCES projects(organization_id,id),
        FOREIGN KEY(organization_id,topic_id) REFERENCES topics(organization_id,id)
    )""",
    """CREATE TABLE research_artifact_draft_chunks (
        draft_id TEXT NOT NULL REFERENCES research_artifact_drafts(id),
        character_offset INTEGER NOT NULL, text TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        PRIMARY KEY(draft_id,character_offset)
    )""",
    """CREATE INDEX research_artifact_drafts_owner
        ON research_artifact_drafts(organization_id,topic_id,created_by,updated_at)""",
    """CREATE TRIGGER research_artifact_draft_chunk_update BEFORE UPDATE ON research_artifact_draft_chunks
        BEGIN SELECT RAISE(ABORT,'file draft chunks are immutable'); END""",
    """CREATE TRIGGER research_artifact_draft_chunk_delete BEFORE DELETE ON research_artifact_draft_chunks
        BEGIN SELECT RAISE(ABORT,'file draft chunks are retained'); END""",
    """CREATE TRIGGER research_artifact_draft_final BEFORE UPDATE ON research_artifact_drafts
        WHEN OLD.result_json IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'completed file drafts are immutable'); END""",
    """CREATE TRIGGER research_artifact_draft_delete BEFORE DELETE ON research_artifact_drafts
        BEGIN SELECT RAISE(ABORT,'file drafts are retained'); END""",
    """CREATE TABLE research_artifact_publications (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, organization_id TEXT NOT NULL, run_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL, revision INTEGER NOT NULL,
        UNIQUE(run_id,artifact_id,revision),
        FOREIGN KEY(organization_id,run_id) REFERENCES llm_runs(organization_id,id),
        FOREIGN KEY(artifact_id,revision) REFERENCES research_artifact_versions(artifact_id,revision)
    )""",
    "CREATE INDEX research_artifact_publications_run ON research_artifact_publications(organization_id,run_id,sequence)",
    """CREATE TRIGGER research_artifact_publication_update BEFORE UPDATE ON research_artifact_publications
        BEGIN SELECT RAISE(ABORT,'file publication receipts are immutable'); END""",
    """CREATE TRIGGER research_artifact_publication_delete BEFORE DELETE ON research_artifact_publications
        BEGIN SELECT RAISE(ABORT,'file publication receipts are retained'); END""",
)
