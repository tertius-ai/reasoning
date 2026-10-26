"""Topic messages added to a running model reply, and the step at which the reply read them."""

SCHEMA = (
    """CREATE TABLE inference_additions (
        organization_id TEXT NOT NULL, message_id TEXT NOT NULL, run_id TEXT NOT NULL,
        added_by TEXT NOT NULL REFERENCES users(id), moved INTEGER NOT NULL DEFAULT 0 CHECK(moved IN (0,1)),
        cancelled_runs_json TEXT NOT NULL DEFAULT '[]', created_at INTEGER NOT NULL, read_at INTEGER, read_step INTEGER,
        PRIMARY KEY(organization_id,message_id),
        FOREIGN KEY(organization_id,run_id) REFERENCES llm_runs(organization_id,id)
    )""",
    "CREATE INDEX inference_addition_run ON inference_additions(run_id,read_at)",
)
