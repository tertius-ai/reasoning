"""Retained PDF extraction coverage and original upload-preview identity."""

SCHEMA = (
    "ALTER TABLE research_media ADD COLUMN extraction_json TEXT NOT NULL DEFAULT '{}'",
)
