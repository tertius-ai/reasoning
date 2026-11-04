"""Identity-bound receipts for explicit recovery of retained file text."""

SCHEMA = (
    """CREATE TABLE research_artifact_recoveries (
        organization_id TEXT NOT NULL, event_sequence INTEGER NOT NULL REFERENCES llm_events(sequence),
        tool_index INTEGER NOT NULL, run_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
        recovered_by TEXT NOT NULL REFERENCES users(id), result_json TEXT NOT NULL, created_at INTEGER NOT NULL,
        PRIMARY KEY(organization_id,event_sequence,tool_index),
        FOREIGN KEY(organization_id,run_id) REFERENCES llm_runs(organization_id,id)
    )""",
)
