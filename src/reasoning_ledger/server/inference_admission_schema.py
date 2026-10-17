"""Indexes for per-call receipt lookups and organization admission checks.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

SCHEMA = (
    # Receipt reads by call alone: each streamed call and every run view look these up.
    "CREATE INDEX inference_call_receipt_lookup ON inference_call_events(call_id,sequence)",
    # Organization balance: reserved/uncertain holds and this month's settled charges.
    "CREATE INDEX llm_run_accounting ON llm_runs(organization_id,accounting_state,settled_at)",
    # Active-run admission limit.
    "CREATE INDEX llm_run_status ON llm_runs(organization_id,status)",
    # Per-requester admission rate limit.
    "CREATE INDEX llm_request_recent ON llm_requests(organization_id,requested_by,created_at)",
)
