"""Millisecond bot wait stages; rows from before these columns keep them null.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

SCHEMA = (
    "ALTER TABLE bot_runs ADD COLUMN enqueued_at_ms INTEGER",
    "ALTER TABLE bot_runs ADD COLUMN claimed_at_ms INTEGER",
    "ALTER TABLE bot_runs ADD COLUMN committed_at_ms INTEGER",
    "ALTER TABLE bot_runs ADD COLUMN runner_timing_json TEXT",
)
