"""Immutable receipts binding each saved file revision to the run that saved it.

An assembled file's receipt lists the pinned section revisions it was joined from.
"""

SCHEMA = (
    """CREATE TABLE research_artifact_publications (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, organization_id TEXT NOT NULL, run_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL, revision INTEGER NOT NULL, assembled_from_json TEXT,
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
