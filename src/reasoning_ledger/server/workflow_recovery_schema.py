"""Admission retirement retains uncertain workflow execution and accounting."""

SCHEMA = (
    "ALTER TABLE workflow_runs ADD COLUMN capacity_released INTEGER NOT NULL DEFAULT 0 CHECK(capacity_released IN (0,1))",
)
