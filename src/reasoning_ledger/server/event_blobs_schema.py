"""Provider requests kept once per distinct message and tool list.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0

A research reply records the full request it sends to the model in every round, and each round repeats the messages
of the rounds before it. ``llm_event_blobs`` keeps each distinct message and tool list once per organization, keyed by
the SHA-256 of its JSON, and a provider-request event refers to them (see ``event_blobs``).

``COMPACT_REQUESTS`` changes nothing when run as SQL. SQLite installations recognise it in
``schema_operations.sqlite_migration`` and rewrite requests recorded inline into that form; PostgreSQL installations
keep earlier requests inline, which readers accept unchanged.
"""

COMPACT_REQUESTS = "UPDATE llm_events SET event_json=event_json WHERE 1=0"

SCHEMA = (
    """CREATE TABLE llm_event_blobs (
        organization_id TEXT NOT NULL REFERENCES organizations(id), sha256 TEXT NOT NULL CHECK(length(sha256)=64),
        body TEXT NOT NULL, PRIMARY KEY(organization_id,sha256)
    )""",
    COMPACT_REQUESTS,
)
